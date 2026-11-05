import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


@contextmanager
def connect(database: Path):
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(database: Path):
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


@dataclass(frozen=True)
class Lease:
    run_id: str
    generation: int


def claim(
    database: Path,
    task_id: str,
    run_id: str,
    *,
    now: float,
    ttl: float = 30,
    budget_owner: str | None = None,
) -> Lease | None:
    if ttl <= 0:
        raise ValueError("租期必须为正")
    with transaction(database) as connection:
        control = connection.execute(
            "SELECT paused FROM global_control WHERE singleton=1"
        ).fetchone()
        holder = connection.execute("SELECT * FROM execution_lease WHERE singleton=1").fetchone()
        task = connection.execute(
            """SELECT tasks.state, tasks.user_excluded, repositories.paused
            FROM tasks JOIN repositories ON repositories.id=tasks.repository_id
            WHERE tasks.id=?""",
            (task_id,),
        ).fetchone()
        if (
            control["paused"]
            or holder["holder_run_id"] is not None
            or task is None
            or task["state"] != "queued"
            or task["user_excluded"]
            or task["paused"]
        ):
            return None
        if connection.execute(
            "SELECT 1 FROM scan_dependencies WHERE waiting_task_id=?", (task_id,)
        ).fetchone():
            return None
        generation = holder["generation"] + 1
        connection.execute(
            """INSERT INTO runs(id,task_id,budget_owner_task_id,generation,snapshot_json,state)
            VALUES(?,?,?,?,'{}','running')""",
            (run_id, task_id, budget_owner or task_id, generation),
        )
        connection.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
        connection.execute(
            """UPDATE execution_lease SET generation=?,holder_run_id=?,expires_at=?,heartbeat_at=?
            WHERE singleton=1""",
            (generation, run_id, now + ttl, now),
        )
        return Lease(run_id, generation)


def require_lease(connection: sqlite3.Connection, lease: Lease, now: float) -> None:
    row = connection.execute("SELECT * FROM execution_lease WHERE singleton=1").fetchone()
    if (
        row["holder_run_id"] != lease.run_id
        or row["generation"] != lease.generation
        or row["expires_at"] <= now
    ):
        raise PermissionError("执行权失效，必须核对旧运行")


def heartbeat(database: Path, lease: Lease, *, now: float, ttl: float = 30) -> None:
    if ttl <= 0:
        raise ValueError("租期必须为正")
    with transaction(database) as connection:
        require_lease(connection, lease, now)
        connection.execute(
            "UPDATE execution_lease SET expires_at=?,heartbeat_at=? WHERE singleton=1",
            (now + ttl, now),
        )


def release(database: Path, lease: Lease, *, now: float, commands_stopped: bool) -> None:
    if not commands_stopped:
        raise PermissionError("旧命令未确认停止，不能释放执行位")
    with transaction(database) as connection:
        require_lease(connection, lease, now)
        connection.execute("UPDATE runs SET state='ended' WHERE id=?", (lease.run_id,))
        connection.execute(
            "UPDATE tasks SET state='ended' WHERE id=(SELECT task_id FROM runs WHERE id=?)",
            (lease.run_id,),
        )
        connection.execute(
            """UPDATE execution_lease SET holder_run_id=NULL,expires_at=NULL,heartbeat_at=NULL
            WHERE singleton=1"""
        )
