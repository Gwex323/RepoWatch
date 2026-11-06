import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate, migration_directory


def test_upgrade_from_p2_preserves_task_and_acceptance_revision(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    settings.data_dir.mkdir()
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_directory()))
    configuration.set_main_option(
        "sqlalchemy.url", "sqlite:///" + settings.business_database.as_posix()
    )
    command.upgrade(configuration, "0018_scan_policy")
    with sqlite3.connect(settings.business_database) as connection:
        connection.execute(
            "INSERT INTO repositories(id,github_id,admission_mode) VALUES('repo','42','all_bugs')"
        )
        connection.execute(
            "INSERT INTO tasks(id,repository_id,source_key,issue_id,state,mode,"
            "acceptance_revision) "
            "VALUES('task','repo','issue:1','1','waiting','triage',5)"
        )
    migrate(settings)
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    try:
        with store.read() as records:
            task = dict(records.one("tasks", id="task"))
            assert task["acceptance_revision"] == 5
            assert records.find("task_acceptance_specs") == []
        TaskRecords(store).operate(
            "spec",
            "task",
            expected_revision=task["revision"],
            action="set_acceptance",
            payload={"kind": "bug", "content": {"objective": "fix", "criteria": ["probe"]}},
            now=1,
        )
        with store.read() as records:
            assert records.one("task_acceptance_specs", task_id="task", revision=6)
    finally:
        store.close()


def test_distillation_consumption_keeps_source_immutable(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    with sqlite3.connect(settings.business_database) as connection:
        connection.execute(
            "INSERT INTO distillation_intents"
            "(id,task_id,session_id,source_sha256,feedback_revision) "
            "VALUES('intent','task','session','source',3)"
        )
        connection.execute("UPDATE distillation_intents SET state='consumed' WHERE id='intent'")
        assert connection.execute(
            "SELECT state,feedback_revision FROM distillation_intents WHERE id='intent'"
        ).fetchone() == ("consumed", 3)
        with pytest.raises(sqlite3.IntegrityError, match="source is immutable"):
            connection.execute(
                "UPDATE distillation_intents SET feedback_revision=4 WHERE id='intent'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM distillation_intents WHERE id='intent'")
