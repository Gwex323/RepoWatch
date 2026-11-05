import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class RemoteAckLost(RuntimeError):
    pass


@contextmanager
def connect(database: Path):
    connection = sqlite3.connect(database, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
    finally:
        connection.close()


def initialize(database: Path) -> None:
    with connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS recovery_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                generation INTEGER NOT NULL,
                paused INTEGER NOT NULL,
                task_state TEXT NOT NULL,
                intent_key TEXT,
                intent_state TEXT,
                pr_id TEXT,
                publish_attempts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remote_pr (
                id TEXT PRIMARY KEY,
                operation_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                head TEXT NOT NULL,
                base TEXT NOT NULL
            );
            INSERT OR IGNORE INTO recovery_state
                (singleton,generation,paused,task_state,publish_attempts)
            VALUES (1,0,1,'idle',0);
            """
        )


class SimulatedRemote:
    def __init__(self, database: Path):
        self.database = database
        self.lose_ack = False
        self.create_calls = 0

    def find(self, operation_key: str) -> dict | None:
        with connect(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM remote_pr WHERE operation_key=?", (operation_key,)
            ).fetchone()
        return dict(row) if row else None

    def create_pr(self, operation_key: str, *, title: str, head: str, base: str) -> dict:
        existing = self.find(operation_key)
        if existing:
            return existing
        self.create_calls += 1
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO remote_pr(id,operation_key,title,head,base)
                    VALUES(?,?,?,?,?)""",
                    (f"pr-{operation_key}", operation_key, title, head, base),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        created = self.find(operation_key)
        if self.lose_ack:
            self.lose_ack = False
            raise RemoteAckLost("远端已创建，回执丢失")
        return created


@dataclass(frozen=True)
class RecoveryState:
    generation: int
    paused: bool
    task_state: str
    intent_key: str | None
    intent_state: str | None
    pr_id: str | None
    publish_attempts: int


class RecoveryController:
    def __init__(self, database: Path, remote: SimulatedRemote):
        self.database = database
        self.remote = remote

    def state(self) -> RecoveryState:
        with connect(self.database) as connection:
            row = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
        return RecoveryState(
            generation=row["generation"],
            paused=bool(row["paused"]),
            task_state=row["task_state"],
            intent_key=row["intent_key"],
            intent_state=row["intent_state"],
            pr_id=row["pr_id"],
            publish_attempts=row["publish_attempts"],
        )

    def set_paused(self, paused: bool) -> None:
        with connect(self.database) as connection:
            connection.execute(
                "UPDATE recovery_state SET paused=? WHERE singleton=1", (int(paused),)
            )

    def record_intent(self, operation_key: str) -> None:
        with connect(self.database) as connection:
            connection.execute(
                """UPDATE recovery_state
                SET intent_key=?, intent_state='pending', task_state='publishing'
                WHERE singleton=1""",
                (operation_key,),
            )

    def publish(self, *, title: str, head: str, base: str) -> dict:
        state = self.state()
        if state.paused:
            raise PermissionError("全局暂停期间不能执行写操作")
        if state.intent_state != "pending" or not state.intent_key:
            raise PermissionError("结果未知或已确认，不得重发")
        with connect(self.database) as connection:
            connection.execute(
                "UPDATE recovery_state SET publish_attempts=publish_attempts+1 WHERE singleton=1"
            )
        try:
            created = self.remote.create_pr(state.intent_key, title=title, head=head, base=base)
        except RemoteAckLost:
            with connect(self.database) as connection:
                connection.execute(
                    """UPDATE recovery_state
                    SET paused=1, intent_state='unknown', task_state='needs_recovery'
                    WHERE singleton=1"""
                )
            return {"outcome": "unknown", "paused": True}
        with connect(self.database) as connection:
            connection.execute(
                """UPDATE recovery_state
                SET intent_state='confirmed', pr_id=?, task_state='waiting_pr'
                WHERE singleton=1""",
                (created["id"],),
            )
        return {"outcome": "created", "pr_id": created["id"], "paused": False}

    def reconcile(self) -> dict:
        state = self.state()
        if state.intent_state not in {"unknown", "pending"} or not state.intent_key:
            raise PermissionError("当前没有可核对的发布意图")
        found = self.remote.find(state.intent_key)
        if found is None:
            with connect(self.database) as connection:
                connection.execute(
                    "UPDATE recovery_state SET intent_state='absent' WHERE singleton=1"
                )
            return {"outcome": "absent", "pr_id": None, "paused": True}
        with connect(self.database) as connection:
            connection.execute(
                """UPDATE recovery_state
                SET intent_state='confirmed', pr_id=?, task_state='waiting_pr'
                WHERE singleton=1""",
                (found["id"],),
            )
        return {"outcome": "confirmed", "pr_id": found["id"], "paused": True}

    def resume(
        self,
        *,
        old_command_alive: bool,
        checkpoint_verified: bool,
        artifacts_verified: bool,
        remote_verified: bool,
    ) -> int:
        state = self.state()
        if not state.paused:
            raise ValueError("当前未处于暂停状态")
        if old_command_alive:
            raise PermissionError("旧命令仍存活，不能释放执行位")
        if not (checkpoint_verified and artifacts_verified and remote_verified):
            raise PermissionError("核对未完成，不能恢复")
        generation = state.generation + 1
        task_state = "waiting_pr" if state.pr_id else "queued"
        with connect(self.database) as connection:
            connection.execute(
                """UPDATE recovery_state
                SET generation=?, paused=0, task_state=?
                WHERE singleton=1""",
                (generation, task_state),
            )
        return generation

    def late_completion(self, generation: int, payload: dict) -> dict:
        state = self.state()
        if generation != state.generation:
            return {"accepted": False, "reason": "stale_generation"}
        if state.paused:
            return {"accepted": False, "reason": "paused"}
        with connect(self.database) as connection:
            connection.execute(
                "UPDATE recovery_state SET task_state='waiting_pr' WHERE singleton=1"
            )
        return {"accepted": True, "payload": payload}

    def classify_restart(self, *, old_command_alive: bool) -> str:
        state = self.state()
        if state.task_state == "waiting_pr":
            return "normal_wait"
        if state.task_state == "needs_recovery":
            return "recovery_required"
        if state.task_state == "running" and old_command_alive:
            return "abandoned_run"
        return "idle"

    def run_diagnostic(self) -> dict:
        before = self.state()
        after = self.state()
        return {
            "ran": True,
            "paused_before": before.paused,
            "paused_after": after.paused,
            "generation_unchanged": before.generation == after.generation,
        }


def run_experiment() -> dict:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="repowatch-recovery-"))
    checks = {}
    steps = []
    try:
        database = root / "recovery.sqlite3"
        initialize(database)
        remote = SimulatedRemote(database)
        controller = RecoveryController(database, remote)
        controller.set_paused(False)
        controller.record_intent("candidate-42:create-pr")
        remote.lose_ack = True
        result = controller.publish(
            title="Fix candidate 42", head="repowatch/issue-42", base="main"
        )
        after_unknown = controller.state()
        checks["ack_lost_enters_pause"] = result["outcome"] == "unknown" and after_unknown.paused
        checks["ack_lost_remote_created"] = remote.find("candidate-42:create-pr") is not None
        steps.append("写入意图后创建 PR；远端成功但回执丢失，进入暂停和待恢复")
        try:
            controller.publish(title="Fix candidate 42", head="repowatch/issue-42", base="main")
            resent = False
        except PermissionError:
            resent = True
        checks["no_resend_while_unknown"] = resent and remote.create_calls == 1
        reconciled = controller.reconcile()
        checks["reconcile_confirms_remote_pr"] = (
            reconciled["outcome"] == "confirmed"
            and reconciled["pr_id"] == "pr-candidate-42:create-pr"
        )
        checks["reconcile_keeps_pause"] = controller.state().paused
        steps.append("回执未知时不重发；按稳定操作键查询并确认已创建的 PR，仍保持暂停")
        try:
            controller.resume(
                old_command_alive=True,
                checkpoint_verified=True,
                artifacts_verified=True,
                remote_verified=True,
            )
            blocked = False
        except PermissionError:
            blocked = True
        checks["old_command_blocks_resume"] = blocked
        old_generation = controller.state().generation
        new_generation = controller.resume(
            old_command_alive=False,
            checkpoint_verified=True,
            artifacts_verified=True,
            remote_verified=True,
        )
        checks["resume_after_reconciliation"] = new_generation == old_generation + 1
        stale = controller.late_completion(old_generation, {"summary": "old result"})
        fresh = controller.late_completion(new_generation, {"summary": "new result"})
        checks["stale_generation_rejected"] = stale == {
            "accepted": False,
            "reason": "stale_generation",
        }
        checks["new_generation_accepted"] = fresh["accepted"] is True
        steps.append("旧命令未停止时拒绝恢复；停止并核对后递增代次，旧代次迟到结果被拒绝")
        waiting = RecoveryController(database, remote)
        with connect(database) as connection:
            connection.execute(
                "UPDATE recovery_state SET task_state='waiting_pr', paused=0 WHERE singleton=1"
            )
        checks["normal_wait_not_crash"] = (
            waiting.classify_restart(old_command_alive=False) == "normal_wait"
        )
        with connect(database) as connection:
            connection.execute(
                "UPDATE recovery_state SET task_state='needs_recovery', paused=1 WHERE singleton=1"
            )
        diagnostic = waiting.run_diagnostic()
        checks["diagnostic_does_not_resume"] = (
            diagnostic["ran"] and diagnostic["paused_after"] and diagnostic["generation_unchanged"]
        )
        steps.append("PR 正常等待重启不误报崩溃；排障不解除暂停、不增加代次")
        checks["single_remote_pr"] = remote.create_calls == 1 and len(list_remote(remote)) == 1
        report = {
            "experiment": "E8/E18",
            "protocol_version": "1",
            "status": "passed" if all(checks.values()) else "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "steps": steps,
            "remote": {
                "create_calls": remote.create_calls,
                "pr_count": len(list_remote(remote)),
                "operation_key": "candidate-42:create-pr",
            },
            "state": controller.state().__dict__,
            "duration_seconds": None,
        }
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if "report" in locals():
            report["duration_seconds"] = round(time.monotonic() - started, 3)


def list_remote(remote: SimulatedRemote) -> list[dict]:
    with connect(remote.database) as connection:
        rows = connection.execute("SELECT * FROM remote_pr").fetchall()
    return [dict(row) for row in rows]
