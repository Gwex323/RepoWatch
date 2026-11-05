import multiprocessing
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from repowatch.adapters.storage.prototype import (
    BUSY_TIMEOUT_MS,
    Lease,
    claim,
    connect,
    heartbeat,
    release,
    require_lease,
    transaction,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "business.sqlite3"
    backend = Path(__file__).resolve().parents[1]
    configuration = Config(str(backend / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(configuration, "head")
    with transaction(path) as connection:
        connection.execute("UPDATE global_control SET paused=0")
        connection.execute(
            "INSERT INTO repositories(id,github_id,admission_mode) VALUES('repo-1','123','manual')"
        )
        for task_id in ("task-1", "task-2", "task-3"):
            connection.execute(
                """INSERT INTO tasks(id,repository_id,source_key,issue_id,state,mode)
                VALUES(?,'repo-1',?,?,'queued','implement')""",
                (task_id, task_id, task_id),
            )
            connection.execute(
                """INSERT INTO budget_ledgers(owner_task_id,limits_json,used_json,reserved_json)
                VALUES(?,'{}','{}','{}')""",
                (task_id,),
            )
    return path


def compete(database, task_id, gate, results):
    if not gate.wait(15):
        raise TimeoutError("竞争实验启动超时")
    lease = claim(database, task_id, f"run-{task_id}", now=100)
    results.put(lease)


def test_two_processes_only_one_holder(database):
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=compete, args=(database, task_id, gate, results))
        for task_id in ("task-1", "task-2")
    ]
    try:
        for process in processes:
            process.start()
        gate.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        assert sum(outcome is not None for outcome in outcomes) == 1
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()


def test_pragmas_and_foreign_key(database):
    with connect(database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO pending_signals(id,task_id,source_key,revision) "
                "VALUES('s','bad','s','1')"
            )


def test_issue_and_managed_pr_lifetime_unique(database):
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO managed_pull_requests "
            "VALUES('task-1','repo-1','task-1','fork','pr-1','closed')"
        )
        for statement in (
            "INSERT INTO managed_pull_requests "
            "VALUES('task-1','repo-1','task-1','fork','pr-2','open')",
            "INSERT INTO managed_pull_requests "
            "VALUES('task-2','repo-1','task-2','fork','pr-1','open')",
            "INSERT INTO managed_pull_requests "
            "VALUES('task-2','repo-1','task-3','fork','pr-3','open')",
            """INSERT INTO tasks(id,repository_id,source_key,issue_id,state,mode)
            VALUES('duplicate','repo-1','other','task-1','queued','implement')""",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


def test_heartbeat_independent_connection_and_no_expired_takeover(database):
    lease = claim(database, "task-1", "run-1", now=100, ttl=5)
    heartbeat(database, lease, now=104, ttl=10)
    with connect(database) as connection:
        assert connection.execute("SELECT expires_at FROM execution_lease").fetchone()[0] == 114
    assert claim(database, "task-2", "run-2", now=120) is None
    with pytest.raises(PermissionError):
        heartbeat(database, lease, now=120)


def test_unstopped_command_and_old_generation_cannot_write(database):
    lease = claim(database, "task-1", "run-1", now=100)
    with pytest.raises(PermissionError):
        release(database, lease, now=101, commands_stopped=False)
    assert claim(database, "task-2", "run-2", now=101) is None
    release(database, lease, now=102, commands_stopped=True)
    current = claim(database, "task-2", "run-2", now=103)
    assert current.generation == lease.generation + 1
    with transaction(database) as connection, pytest.raises(PermissionError):
        require_lease(connection, Lease("run-1", lease.generation), 104)


def test_global_pause_prevents_claim(database):
    with transaction(database) as connection:
        connection.execute("UPDATE global_control SET paused=1")
    assert claim(database, "task-1", "run-1", now=100) is None
