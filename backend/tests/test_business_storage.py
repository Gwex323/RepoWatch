import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from alembic import command
from alembic.config import Config
from langgraph.graph import END, START, StateGraph

from repowatch.adapters.storage.checkpoints import checkpoint_store
from repowatch.adapters.storage.sqlite import SCHEMA_REVISION, SQLiteBusinessStore
from repowatch.application.storage import ConflictError, SchemaMismatch
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate, migration_directory


@pytest.fixture
def settings(tmp_path):
    configuration = StorageSettings(tmp_path / "data")
    migrate(configuration)
    return configuration


def repository(records, identifier="repo"):
    records.add("repositories", {"id": identifier, "github_id": "123", "admission_mode": "manual"})


def test_configuration_has_no_io_and_requires_shared_absolute_location(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOWATCH_DATA_DIR", str(tmp_path / "shared"))
    settings = StorageSettings.from_environment()
    assert settings.data_dir == tmp_path / "shared"
    assert not settings.data_dir.exists()
    assert (
        len({settings.business_database, settings.checkpoint_database, settings.index_database})
        == 3
    )
    with pytest.raises(ValueError, match="绝对路径"):
        StorageSettings(Path("relative"))
    with pytest.raises(ValueError, match="锁等待"):
        StorageSettings(tmp_path, busy_timeout_ms=0)


def test_runtime_refuses_missing_or_incompatible_schema_without_initializing(tmp_path, settings):
    missing = StorageSettings(tmp_path / "absent")
    with pytest.raises(SchemaMismatch):
        SQLiteBusinessStore(missing)
    with pytest.raises(SchemaMismatch):
        with checkpoint_store(missing):
            pass
    assert not missing.data_dir.exists()
    with sqlite3.connect(settings.business_database) as connection:
        connection.execute("UPDATE alembic_version SET version_num='future'")
    with pytest.raises(SchemaMismatch):
        SQLiteBusinessStore(settings)
    with sqlite3.connect(settings.business_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "future",
        )


def test_migration_is_repeatable_and_pragmas_apply_to_every_connection(settings):
    store = SQLiteBusinessStore(settings)
    with store.write() as records:
        repository(records)
    store.close()
    migrate(settings)
    restarted = SQLiteBusinessStore(settings)
    with restarted.read() as records:
        assert records.one("repositories", id="repo")["github_id"] == "123"
    for _ in range(2):
        with restarted.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
    restarted.close()


def test_upgrade_from_p0_preserves_existing_records(tmp_path):
    settings = StorageSettings(tmp_path)
    config = Config()
    config.set_main_option("script_location", str(migration_directory()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.business_database.as_posix()}")
    command.upgrade(config, "0001_core_draft")
    with sqlite3.connect(settings.business_database) as connection:
        connection.execute("INSERT INTO repositories VALUES('existing','42',1,1,1,'labels',1)")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    with store.read() as records:
        assert records.one("repositories", id="existing")["paused"] == 1
    with sqlite3.connect(settings.business_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_business_changes_and_events_commit_or_rollback_together(settings):
    store = SQLiteBusinessStore(settings)
    with pytest.raises(ConflictError):
        with store.write() as records:
            repository(records)
            records.add("business_events", {"kind": "created", "payload_json": "{}"})
            repository(records, "duplicate")
    with store.read() as records:
        assert records.find("repositories") == []
        assert records.find("business_events") == []
        with pytest.raises(PermissionError):
            repository(records)
    with store.write() as records:
        repository(records)
        records.add("business_events", {"kind": "created", "payload_json": "{}"})
    with store.write() as records:
        assert records.change("repositories", {"id": "repo"}, {"paused": 1}) == 1
        with pytest.raises(ValueError):
            records.remove("repositories")
    store.close()
    restarted = SQLiteBusinessStore(settings)
    with restarted.read() as records:
        assert records.one("repositories", id="repo")["paused"] == 1
        assert len(records.find("business_events")) == 1
    restarted.close()


def test_new_process_reads_committed_business_records(settings):
    store = SQLiteBusinessStore(settings)
    with store.write() as records:
        repository(records)
    store.close()
    script = """
import sys
from pathlib import Path
from repowatch.configuration import StorageSettings
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
store = SQLiteBusinessStore(StorageSettings(Path(sys.argv[1])))
with store.read() as records:
    assert records.one('repositories', id='repo')['github_id'] == '123'
    records_count = len(records.find('repositories'))
store.close()
print(records_count)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(settings.data_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "1"


class GraphState(TypedDict):
    value: int


def graph(saver):
    builder = StateGraph(GraphState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=saver)


def test_graph_checkpoint_survives_restart_and_is_separate_from_business(settings):
    config = {"configurable": {"thread_id": "run-1"}}
    with checkpoint_store(settings) as saver:
        assert graph(saver).invoke({"value": 4}, config)["value"] == 5
    with checkpoint_store(settings) as saver:
        assert graph(saver).get_state(config).values == {"value": 5}
        assert saver.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert saver.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with sqlite3.connect(settings.business_database) as business:
        assert (
            business.execute("SELECT name FROM sqlite_master WHERE name='checkpoints'").fetchall()
            == []
        )
    with sqlite3.connect(settings.checkpoint_database) as checkpoints:
        assert (
            checkpoints.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchall()
            == []
        )


def test_checkpoint_commit_is_not_rolled_back_with_business_transaction(settings):
    store = SQLiteBusinessStore(settings)
    with pytest.raises(RuntimeError, match="模拟中断"):
        with store.write() as records:
            repository(records)
            with checkpoint_store(settings) as saver:
                graph(saver).invoke({"value": 1}, {"configurable": {"thread_id": "orphan"}})
            raise RuntimeError("模拟中断")
    with store.read() as records:
        assert records.find("repositories") == []
    with checkpoint_store(settings) as saver:
        assert graph(saver).get_state({"configurable": {"thread_id": "orphan"}}).values == {
            "value": 2,
        }
    store.close()


def test_incompatible_checkpoint_schema_is_refused_without_implicit_upgrade(settings):
    with sqlite3.connect(settings.checkpoint_database) as connection:
        connection.execute("UPDATE repowatch_schema SET package_version='unverified'")
    with pytest.raises(SchemaMismatch):
        with checkpoint_store(settings):
            pass
    with pytest.raises(SchemaMismatch):
        migrate(settings)
    with sqlite3.connect(settings.checkpoint_database) as connection:
        assert connection.execute("SELECT package_version FROM repowatch_schema").fetchone() == (
            "unverified",
        )


@pytest.mark.skipif(not hasattr(os, "link"), reason="宿主不支持硬链接")
def test_database_hardlink_alias_is_rejected(tmp_path):
    (tmp_path / "business.sqlite3").touch()
    os.link(tmp_path / "business.sqlite3", tmp_path / "checkpoints.sqlite3")
    with pytest.raises(ValueError, match="硬链接"):
        StorageSettings(tmp_path)
