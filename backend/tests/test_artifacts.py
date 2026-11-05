import json
import sqlite3

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import ArtifactService, EventReader
from repowatch.application.budgets import BudgetedExecutor, BudgetService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.recovery import RecoveryService
from repowatch.application.stages import StageResults
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.operations.migrate import migrate


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repo = tasks.register_repository("r", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, "issue:1", now=0)
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "initial",
        task,
        BudgetAmounts(
            active_ms=10000,
            model_calls=10,
            tokens=1000,
            repair_rounds=10,
            recovery_rounds=10,
        ),
        expected_revision=1,
        reason="验收",
        now=0,
    )
    queue.set_pause(False, now=0)
    token = queue.claim(now=0)
    yield settings, store, queue, budgets, ArtifactService(store, settings.artifacts_dir), token
    store.close()


def test_immutable_registration_digest_scope_and_database_identity(kernel):
    settings, store, _, _, artifacts, token = kernel
    identifier = artifacts.register(token, "log", b"actual-output", context={"kind": "test"}, now=0)
    assert (
        artifacts.register(token, "log", b"actual-output", context={"kind": "test"}, now=0)
        == identifier
    )
    row = artifacts.require(identifier, run_id=token.run_id, now=0)
    assert (settings.artifacts_dir / row["relative_path"]).read_bytes() == b"actual-output"
    assert len(json.loads(row["context_json"])["snapshot_sha256"]) == 64
    with pytest.raises(ConflictError):
        artifacts.register(token, "log", b"new", context={"kind": "test"}, now=0)
    with pytest.raises(PermissionError):
        artifacts.require(identifier, run_id="other", now=0)
    for change in ({"sha256": "0" * 64}, {"context_json": "{}"}, {"run_id": "other"}):
        with pytest.raises(ConflictError):
            with store.write() as records:
                records.change("artifacts", {"id": identifier}, change)
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.remove("artifacts", id=identifier)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_missing_or_changed_artifact_blocks_and_never_reactivates_old_evidence(kernel, damage):
    settings, store, queue, _, artifacts, token = kernel
    identifier = artifacts.register(token, "result", b"proof", context={}, now=0)
    row = artifacts.require(identifier, now=0)
    path = settings.artifacts_dir / row["relative_path"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    with pytest.raises(PermissionError):
        artifacts.require(identifier, now=1)
    with store.read() as records:
        assert records.one("artifacts", id=identifier)["available"] == 0
        assert records.one("global_control", singleton=1)["paused"] == 1
    with pytest.raises(PermissionError):
        artifacts.register(token, "result", b"proof", context={}, now=1)
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.change("artifacts", {"id": identifier}, {"available": 1})
    assert not RecoveryService(store, settings).report(token.run_id, now=1)["facts"]["artifacts"][
        0
    ]["valid"]
    with pytest.raises(PermissionError):
        queue.guard(token, now=1, purpose="publish")


def test_partial_disk_failure_stops_tools_and_publication(kernel, monkeypatch):
    _, store, queue, budgets, artifacts, token = kernel

    def no_space(*_args, **_kwargs):
        raise OSError("injected disk full")

    monkeypatch.setattr("repowatch.adapters.storage.artifact_files.os.fsync", no_space)
    with pytest.raises(StorageError):
        artifacts.register(token, "log", b"proof", context={}, now=0)
    calls = []
    executor = BudgetedExecutor(budgets, queue, clock=lambda: 0)
    for purpose in ("tool", "publish"):
        with pytest.raises(StorageError):
            executor.execute(
                token,
                purpose,
                BudgetAmounts(active_ms=1000),
                lambda value=purpose: calls.append(value),
                lambda _: {},
                purpose=purpose,
            )
    assert calls == []
    with store.read() as records:
        assert records.find("artifacts") == []
        assert records.find("artifact_intents")[0]["state"] == "pending"


def test_file_saved_database_commit_failure_is_reconciled_without_replaying(kernel):
    settings, store, _, _, artifacts, token = kernel

    def fail_registration(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO artifacts "):
            raise OperationalError(statement, {}, sqlite3.OperationalError("injected disk full"))

    event.listen(store.engine, "before_cursor_execute", fail_registration)
    with pytest.raises(StorageError):
        artifacts.register(token, "proof", b"saved", context={}, now=0)
    event.remove(store.engine, "before_cursor_execute", fail_registration)
    reopened = SQLiteBusinessStore(settings)
    try:
        budgets = BudgetService(reopened, settings.artifacts_dir)
        assert not budgets.reserve(
            token,
            "next",
            BudgetAmounts(active_ms=1000),
            purpose="tool",
            now=0,
        ).allowed
        recovered = ArtifactService(reopened, settings.artifacts_dir)
        result = recovered.reconcile(token, "proof", now=0)
        assert result["registered"]
        recovered.require(result["artifact_id"], run_id=token.run_id, now=0)
        assert budgets.reserve(
            token,
            "after",
            BudgetAmounts(active_ms=1000),
            purpose="tool",
            now=0,
        ).allowed
    finally:
        reopened.close()


def test_business_intent_failure_never_starts_actual_operation(kernel):
    _, store, queue, budgets, _, token = kernel

    def fail_intent(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO budget_step_plans"):
            raise OperationalError(statement, {}, sqlite3.OperationalError("injected read only"))

    event.listen(store.engine, "before_cursor_execute", fail_intent)
    calls = []
    with pytest.raises(StorageError):
        BudgetedExecutor(budgets, queue, clock=lambda: 0).execute(
            token,
            "tool",
            BudgetAmounts(active_ms=1000),
            lambda: calls.append(True),
            lambda _: {},
            purpose="tool",
        )
    event.remove(store.engine, "before_cursor_execute", fail_intent)
    with pytest.raises(StorageError):
        queue.guard(token, now=0, purpose="publish")
    assert calls == []


def test_stage_cannot_promote_dangling_or_corrupt_reference(kernel):
    settings, store, _, _, artifacts, token = kernel
    stages = StageResults(store, artifacts)
    stage_id = stages.begin(token, "verify", now=0)
    with pytest.raises(LookupError):
        stages.save(token, stage_id, artifact_ids=["absent"], result={"passed": True}, now=0)
    identifier = artifacts.register(token, "proof", b"actual", context={}, now=0)
    row = artifacts.require(identifier, now=0)
    (settings.artifacts_dir / row["relative_path"]).unlink()
    with pytest.raises(PermissionError):
        stages.save(token, stage_id, artifact_ids=[identifier], result={"passed": True}, now=0)
    with store.read() as records:
        assert records.one("stage_intents", id=stage_id)["state"] == "intent"


def test_event_cursor_reconnect_order_filter_and_bounded_read(kernel):
    settings, store, _, _, artifacts, token = kernel
    artifacts.register(token, "event", b"proof", context={}, now=0)
    reader = EventReader(store)
    first = reader.read(limit=2)
    second = reader.read(after=first["cursor"], limit=500)
    sequences = [row["sequence"] for row in first["events"] + second["events"]]
    assert sequences == sorted(set(sequences))
    assert reader.read(after=second["cursor"])["events"] == []
    restarted = SQLiteBusinessStore(settings)
    try:
        assert EventReader(restarted).read(after=first["cursor"]) == second
    finally:
        restarted.close()
    assert reader.read(task_id="other")["events"] == []
    for options in ({"after": -1}, {"limit": 501}, {"after": True}):
        with pytest.raises(ValueError):
            reader.read(**options)
