import json
import sqlite3

from alembic import command
from alembic.config import Config

from repowatch.adapters.storage.sqlite import SCHEMA_REVISION, SQLiteBusinessStore
from repowatch.application.budgets import BudgetService
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate, migration_directory


def test_upgrade_from_p1_task_records_preserves_usage_waits_and_closed_pr(tmp_path):
    settings = StorageSettings(tmp_path)
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_directory()))
    configuration.set_main_option(
        "sqlalchemy.url", f"sqlite:///{settings.business_database.as_posix()}"
    )
    command.upgrade(configuration, "0003_task_records")
    original_used = '{"active_ms":50,"model_calls":2,"tokens":21,"repair_rounds":1}'
    original_snapshot = '{"mode":"triage","input":{"title":"保留旧快照"}}'
    with sqlite3.connect(settings.business_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO repositories(id,github_id,admission_mode,paused) "
            "VALUES('r','123','all_bugs',1)"
        )
        connection.execute("""INSERT INTO tasks
            (id,repository_id,source_key,issue_id,state,mode,input_revision,feedback_revision,
             user_excluded,revision,wait_reason,budget_owner_task_id)
            VALUES('t','r','issue:1','issue-id','waiting','triage',3,2,1,7,'review','t')""")
        connection.execute("INSERT INTO task_sources VALUES('r','issue:1','t')")
        connection.execute(
            """INSERT INTO budget_ledgers
            (owner_task_id,revision,limits_json,used_json,reserved_json)
            VALUES('t',4,'{}',?,'{}')""",
            (original_used,),
        )
        connection.execute(
            """INSERT INTO runs
            (id,task_id,budget_owner_task_id,generation,snapshot_json,state,wait_reason,graph_thread_id)
            VALUES('old-run','t','t',5,?,'suspended','review','old-thread')""",
            (original_snapshot,),
        )
        connection.execute(
            "INSERT INTO managed_pull_requests VALUES('t','r','issue-id','fork','pr-id','closed')"
        )
        connection.execute("UPDATE execution_lease SET generation=5")
        connection.execute("UPDATE global_control SET paused=1,revision=4")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    try:
        with store.read() as records:
            task = records.one("tasks", id="t")
            assert (
                task["state"],
                task["wait_reason"],
                task["user_excluded"],
                task["revision"],
            ) == (
                "waiting",
                "review",
                1,
                7,
            )
            assert records.one("runs", id="old-run")["snapshot_json"] == original_snapshot
            assert records.one("budget_ledgers", owner_task_id="t")["used_json"] == original_used
            assert records.one("managed_pull_requests", task_id="t")["state"] == "closed"
            assert records.one("execution_lease", singleton=1)["generation"] == 5
            assert records.one("global_control", singleton=1)["paused"] == 1
        view = BudgetService(store, settings.artifacts_dir).view("t")
        assert view["status"] == "configuration_required"
        assert view["used"]["tokens"] == 21
        assert view["used"]["model_calls"] == 2
        assert view["approved_progress_ids"] == []
        with sqlite3.connect(settings.business_database) as connection:
            assert (
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                == SCHEMA_REVISION
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert (
                json.loads(
                    connection.execute("SELECT reserved_json FROM budget_ledgers").fetchone()[0]
                )
                == {}
            )
    finally:
        store.close()
