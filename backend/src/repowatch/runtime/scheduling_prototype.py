import json
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

from repowatch.adapters.storage.prototype import (
    Lease,
    claim,
    connect,
    release,
    transaction,
)


def create_database(path: Path) -> None:
    backend = Path(__file__).resolve().parents[3]
    configuration = Config(str(backend / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(configuration, "head")
    with transaction(path) as connection:
        connection.execute("UPDATE global_control SET paused=1")


def add_task(database: Path, task_id: str, *, mode: str = "implement") -> None:
    with transaction(database) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO repositories(id,github_id,admission_mode)
            VALUES('repo-1','1','manual')"""
        )
        connection.execute(
            """INSERT INTO tasks(id,repository_id,source_key,issue_id,state,mode)
            VALUES(?,'repo-1',?,?,'queued',?)""",
            (task_id, task_id, task_id, mode),
        )
        connection.execute(
            """INSERT INTO budget_ledgers(owner_task_id,limits_json,used_json,reserved_json)
            VALUES(?,'{}','{}','{}')""",
            (task_id,),
        )


def add_dependency(database: Path, waiting_task: str, scan_task: str) -> None:
    with transaction(database) as connection:
        connection.execute(
            "INSERT INTO scan_dependencies(waiting_task_id,scan_task_id) VALUES(?,?)",
            (waiting_task, scan_task),
        )
        connection.execute("UPDATE tasks SET state='waiting' WHERE id=?", (waiting_task,))


def is_paused(database: Path) -> bool:
    with connect(database) as connection:
        return bool(
            connection.execute("SELECT paused FROM global_control WHERE singleton=1").fetchone()[
                "paused"
            ]
        )


def save_sync_signal(database: Path, signal_id: str) -> dict:
    if not is_paused(database):
        raise PermissionError("只读同步的暂停语义要求全局暂停")
    with transaction(database) as connection:
        connection.execute(
            "INSERT INTO business_events(kind,payload_json) VALUES('sync_signal',?)",
            (json.dumps({"signal_id": signal_id}),),
        )
    return {"saved": True, "model_calls": 0}


def request_model_call(database: Path, task_id: str) -> dict:
    if is_paused(database):
        raise PermissionError("全局暂停期间不得调用模型")
    with transaction(database) as connection:
        connection.execute(
            "INSERT INTO business_events(task_id,kind,payload_json) VALUES(?,'model_call','{}')",
            (task_id,),
        )
    return {"called": True}


def count_events(database: Path, kind: str) -> int:
    with connect(database) as connection:
        return connection.execute(
            "SELECT COUNT(*) AS total FROM business_events WHERE kind=?", (kind,)
        ).fetchone()["total"]


def complete_scan(database: Path, scan_task: str, lease: Lease, *, now: float) -> dict:
    scan_run = lease.run_id
    with transaction(database) as connection:
        holder = connection.execute(
            "SELECT holder_run_id FROM execution_lease WHERE singleton=1"
        ).fetchone()["holder_run_id"]
        if holder != scan_run:
            raise PermissionError("扫描运行不持有执行位")
        connection.execute("UPDATE runs SET state='ended' WHERE id=?", (scan_run,))
        connection.execute("UPDATE tasks SET state='ended' WHERE id=?", (scan_task,))
        waiters = [
            row["waiting_task_id"]
            for row in connection.execute(
                "SELECT waiting_task_id FROM scan_dependencies WHERE scan_task_id=?",
                (scan_task,),
            ).fetchall()
        ]
        connection.execute("DELETE FROM scan_dependencies WHERE scan_task_id=?", (scan_task,))
        woken = []
        for waiter in waiters:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM scan_dependencies WHERE waiting_task_id=?",
                (waiter,),
            ).fetchone()["total"]
            if remaining == 0:
                connection.execute("UPDATE tasks SET state='queued' WHERE id=?", (waiter,))
                woken.append(waiter)
    woken.sort()
    release(database, lease, now=now, commands_stopped=True)
    return {"woken": woken, "scan_ended": True}


def cancel_waiter(database: Path, waiting_task: str, *, revoke_budget: bool = False) -> dict:
    with transaction(database) as connection:
        scans = [
            row["scan_task_id"]
            for row in connection.execute(
                "SELECT scan_task_id FROM scan_dependencies WHERE waiting_task_id=?",
                (waiting_task,),
            ).fetchall()
        ]
        connection.execute("DELETE FROM scan_dependencies WHERE waiting_task_id=?", (waiting_task,))
        connection.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (waiting_task,))
        stopped = []
        remaining = {}
        if revoke_budget:
            for scan in scans:
                run = connection.execute(
                    """SELECT runs.id FROM runs JOIN tasks ON tasks.id=runs.task_id
                    WHERE runs.task_id=? AND runs.state='running'
                    AND runs.budget_owner_task_id=?""",
                    (scan, waiting_task),
                ).fetchone()
                if run is None:
                    continue
                connection.execute("UPDATE runs SET state='abandoned' WHERE id=?", (run["id"],))
                connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (scan,))
                connection.execute(
                    """UPDATE tasks SET state='blocked' WHERE id IN
                    (SELECT waiting_task_id FROM scan_dependencies WHERE scan_task_id=?)""",
                    (scan,),
                )
                connection.execute(
                    """UPDATE execution_lease
                    SET holder_run_id=NULL, expires_at=NULL, heartbeat_at=NULL
                    WHERE holder_run_id=?""",
                    (run["id"],),
                )
                stopped.append(scan)
                remaining[scan] = [
                    row["waiting_task_id"]
                    for row in connection.execute(
                        "SELECT waiting_task_id FROM scan_dependencies WHERE scan_task_id=?",
                        (scan,),
                    ).fetchall()
                ]
    return {"scan_stopped": stopped, "remaining_waiters": remaining}


def classify_run(database: Path, run_id: str) -> str:
    with connect(database) as connection:
        run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        holder = connection.execute(
            "SELECT holder_run_id FROM execution_lease WHERE singleton=1"
        ).fetchone()["holder_run_id"]
    if run["state"] == "suspended" and run["wait_reason"]:
        return "normal_wait"
    if run["state"] == "running" and holder != run_id:
        return "abandoned_run"
    return run["state"]


def add_waiting_run(database: Path, task_id: str, run_id: str, wait_reason: str) -> None:
    with transaction(database) as connection:
        connection.execute(
            """INSERT INTO runs(id,task_id,budget_owner_task_id,generation,snapshot_json,
            state,wait_reason) VALUES(?,?,?,1,'{}','suspended',?)""",
            (run_id, task_id, task_id, wait_reason),
        )
        connection.execute("UPDATE tasks SET state='waiting' WHERE id=?", (task_id,))


def late_write(database: Path, run_id: str) -> dict:
    with connect(database) as connection:
        run = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        holder = connection.execute(
            "SELECT holder_run_id FROM execution_lease WHERE singleton=1"
        ).fetchone()["holder_run_id"]
    if run["state"] != "running" or holder != run_id:
        return {"accepted": False, "reason": "stale_or_released_run"}
    return {"accepted": True}


def run_experiment() -> dict:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="repowatch-scheduling-"))
    checks = {}
    steps = []
    try:
        database = root / "scheduling.sqlite3"
        create_database(database)
        for task in ("scan-1", "repair-a", "repair-b", "independent"):
            add_task(database, task, mode="scan" if task.startswith("scan") else "implement")
        save_sync_signal(database, "issue-100")
        checks["paused_sync_saved"] = count_events(database, "sync_signal") == 1
        try:
            request_model_call(database, "repair-a")
            blocked = False
        except PermissionError:
            blocked = True
        checks["paused_model_blocked"] = blocked and count_events(database, "model_call") == 0
        checks["paused_claim_blocked"] = claim(database, "repair-a", "paused-run", now=1000) is None
        steps.append("暂停时同步只保存信号；模型调用和任务领取均被拒绝")
        with transaction(database) as connection:
            connection.execute("UPDATE global_control SET paused=0")
        add_dependency(database, "repair-a", "scan-1")
        add_dependency(database, "repair-b", "scan-1")
        scan_lease = claim(database, "scan-1", "scan-run-1", now=1001)
        checks["scan_claimed"] = scan_lease is not None
        checks["waiting_repair_blocked"] = (
            claim(database, "repair-a", "repair-run-1", now=1002) is None
        )
        with connect(database) as connection:
            holder = connection.execute(
                "SELECT holder_run_id FROM execution_lease WHERE singleton=1"
            ).fetchone()["holder_run_id"]
        checks["waiting_task_holds_no_lease"] = holder == "scan-run-1"
        completed = complete_scan(database, "scan-1", scan_lease, now=1003)
        checks["atomic_wake_waiters"] = completed["woken"] == ["repair-a", "repair-b"]
        repair_lease = claim(database, "repair-a", "repair-run-2", now=1004)
        checks["no_placeholder_deadlock"] = repair_lease is not None
        release(database, repair_lease, now=1005, commands_stopped=True)
        independent_lease = claim(database, "independent", "independent-run", now=1006)
        checks["independent_task_runs"] = independent_lease is not None
        release(database, independent_lease, now=1007, commands_stopped=True)
        steps.append("等待扫描的修复任务不持有执行位；扫描完成后同一事务唤醒依赖方，随后可正常领取")
        for task in ("scan-2", "repair-c", "repair-d"):
            add_task(database, task, mode="scan" if task.startswith("scan") else "implement")
        add_dependency(database, "repair-c", "scan-2")
        add_dependency(database, "repair-d", "scan-2")
        cancel_waiter(database, "repair-d")
        with connect(database) as connection:
            remaining = connection.execute(
                "SELECT waiting_task_id FROM scan_dependencies WHERE scan_task_id='scan-2'"
            ).fetchall()
        checks["normal_cancel_keeps_shared_scan"] = [
            row["waiting_task_id"] for row in remaining
        ] == ["repair-c"]
        steps.append("普通取消只删除取消方依赖，其他等待方继续共享扫描")
        for task in ("scan-3", "repair-e", "repair-f"):
            add_task(database, task, mode="scan" if task.startswith("scan") else "implement")
        add_dependency(database, "repair-e", "scan-3")
        add_dependency(database, "repair-f", "scan-3")
        revoked_lease = claim(database, "scan-3", "scan-run-3", now=1008, budget_owner="repair-e")
        checks["revoked_scan_claimed"] = revoked_lease is not None
        revoked = cancel_waiter(database, "repair-e", revoke_budget=True)
        checks["budget_revocation_stops_scan"] = revoked["scan_stopped"] == ["scan-3"]
        checks["budget_revocation_keeps_other_dependency"] = revoked["remaining_waiters"] == {
            "scan-3": ["repair-f"]
        }
        checks["revoked_scan_cannot_late_write"] = revoked_lease is not None and late_write(
            database, revoked_lease.run_id
        ) == {"accepted": False, "reason": "stale_or_released_run"}
        steps.append("撤销扫描预算时安全停止扫描，其他依赖保持阻塞，旧运行不能迟到写入")
        add_task(database, "waiting-pr")
        add_waiting_run(database, "waiting-pr", "pr-run", "pr_review")
        checks["normal_wait_not_crash"] = classify_run(database, "pr-run") == "normal_wait"
        steps.append("PR 正常等待重启不误报异常运行")
        report = {
            "experiment": "E13",
            "protocol_version": "1",
            "status": "passed" if all(checks.values()) else "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "steps": steps,
            "counts": {
                "sync_signals": count_events(database, "sync_signal"),
                "model_calls": count_events(database, "model_call"),
            },
            "duration_seconds": None,
        }
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if "report" in locals():
            report["duration_seconds"] = round(time.monotonic() - started, 3)
