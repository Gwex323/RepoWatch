import pytest

from repowatch.adapters.storage.prototype import claim, transaction
from repowatch.runtime.scheduling_prototype import (
    add_task,
    count_events,
    create_database,
    request_model_call,
    run_experiment,
    save_sync_signal,
)


def test_paused_sync_saves_signal_without_model_call(tmp_path):
    database = tmp_path / "scheduling.sqlite3"
    create_database(database)
    add_task(database, "repair-a")
    assert save_sync_signal(database, "issue-1") == {"saved": True, "model_calls": 0}
    with pytest.raises(PermissionError):
        request_model_call(database, "repair-a")
    assert claim(database, "repair-a", "run-1", now=100) is None
    assert count_events(database, "sync_signal") == 1
    assert count_events(database, "model_call") == 0


def test_dependency_blocks_claim_and_scan_releases_slot(tmp_path):
    database = tmp_path / "scheduling.sqlite3"
    create_database(database)
    with transaction(database) as connection:
        connection.execute("UPDATE global_control SET paused=0")
    for task in ("scan", "repair"):
        add_task(database, task, mode="scan" if task == "scan" else "implement")
    with transaction(database) as connection:
        connection.execute(
            "INSERT INTO scan_dependencies(waiting_task_id,scan_task_id) VALUES('repair','scan')"
        )
        connection.execute("UPDATE tasks SET state='waiting' WHERE id='repair'")
    scan_lease = claim(database, "scan", "scan-run", now=100)
    assert scan_lease is not None
    assert claim(database, "repair", "repair-run", now=101) is None
    with pytest.raises(PermissionError):
        save_sync_signal(database, "issue-2")


def test_e13_scenario_report_covers_pause_dependency_and_revocation():
    report = run_experiment()
    assert report["status"] == "passed"
    assert all(report["checks"].values())
    assert report["counts"] == {"sync_signals": 1, "model_calls": 0}
    assert report["duration_seconds"] is not None
