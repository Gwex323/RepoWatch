import json

import pytest

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.budgets import BudgetService
from repowatch.application.control import ControlService
from repowatch.application.diagnostics import DiagnosticService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.recovery import RecoveryService
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts, BudgetDenied
from repowatch.operations.migrate import migrate

LIMITS = BudgetAmounts(
    active_ms=10000,
    model_calls=3,
    tokens=1000,
    repair_rounds=1,
    recovery_rounds=1,
)


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    yield (
        settings,
        store,
        DiagnosticService(store),
        Orchestrator(store),
        BudgetService(
            store,
            settings.artifacts_dir,
        ),
    )
    store.close()


def request(k, key="diag", kind="storage_probe", **kwargs):
    parameters = (
        {} if kind == "storage_probe" else {"provider_revision_id": "saved-v1", "model": "m"}
    )
    return k[2].request(
        key,
        kind,
        parameters,
        LIMITS,
        expected_revision=1,
        now=0,
        **kwargs,
    )


def test_paused_diagnostic_without_any_repository_keeps_pause_and_budget(kernel):
    settings, store, diagnostics, queue, budgets = kernel
    result = request(kernel)
    assert request(kernel) == result
    assert queue.claim(now=0) is None
    token = diagnostics.claim(result["task_id"], now=0)
    spec = diagnostics.specification(token, now=0)
    assert spec["kind"] == "storage_probe"
    with pytest.raises(PermissionError):
        diagnostics.claim(result["task_id"], now=0)
    step = budgets.reserve(token, "probe", BudgetAmounts(active_ms=1000), purpose="tool", now=0)
    budgets.start(token, step.reservation_id, now=0)
    queue.guard(token, now=0, reservation_id=step.reservation_id)
    budgets.settle(token, step.reservation_id, {}, now=0.1)
    diagnostics.finish(token, now=0.2, passed=True, checks={"storage": True})
    assert budgets.view(result["task_id"])["used"]["active_ms"] == 200
    assert queue.claim(now=0.3) is None
    with store.read() as records:
        assert records.find("repositories") == []
        assert records.one("global_control", singleton=1)["paused"] == 1
        snapshot = json.loads(records.one("runs", id=token.run_id)["snapshot_json"])
        assert snapshot["scope"] == "system_diagnostic"
        assert snapshot["target_sha"] is None
    assert RecoveryService(store, settings).scan_startup(now=1)[0]["state"] == "safe_wait"


def test_diagnostic_does_not_clear_business_blockers_or_touch_business_budget(kernel):
    _, store, diagnostics, queue, budgets = kernel
    tasks = TaskRecords(store)
    repo = tasks.register_repository("r", admission_mode="all_bugs")
    business = tasks.ensure_task(repo, "issue:1", now=0)
    before = budgets.view(business)
    with store.write() as records:
        records.add(
            "block_reasons",
            {
                "id": "reason",
                "task_id": business,
                "code": "failed",
                "details_json": "{}",
            },
        )
        records.add(
            "publish_operations",
            {
                "id": "remote",
                "task_id": business,
                "operation_key": "remote",
                "state": "unknown",
                "expected_json": "{}",
            },
        )
    result = request(kernel)
    token = diagnostics.claim(result["task_id"], now=0)
    diagnostics.finish(token, now=1, passed=True, checks={"storage": True})
    assert budgets.view(business) == before
    with store.read() as records:
        assert records.one("block_reasons", id="reason")["resolved"] == 0
        assert records.one("publish_operations", id="remote")["state"] == "unknown"
    assert queue.claim(now=2) is None


def test_diagnostic_has_only_exact_permission_and_bounded_time(kernel):
    _, _, diagnostics, queue, budgets = kernel
    result = request(kernel, kind="provider_connection", duration=5)
    token = diagnostics.claim(result["task_id"], now=0)
    for purpose in ("publish", "tool", "counter"):
        assert not budgets.reserve(
            token,
            purpose,
            BudgetAmounts(repair_rounds=1) if purpose == "counter" else BudgetAmounts(active_ms=1),
            purpose=purpose,
            now=0,
        ).allowed
    reservation = budgets.reserve(
        token,
        "model",
        BudgetAmounts(active_ms=1000, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    budgets.start(token, reservation.reservation_id, now=0)
    with pytest.raises(PermissionError):
        queue.guard(token, now=0, purpose="publish", reservation_id=reservation.reservation_id)
    budgets.settle(token, reservation.reservation_id, {"model_calls": 1, "tokens": 5}, now=0.5)
    queue.heartbeat(token, now=1, ttl=30)
    assert not budgets.reserve(
        token,
        "late",
        BudgetAmounts(active_ms=1, model_calls=1, tokens=10),
        purpose="model",
        now=5,
    ).allowed


def test_diagnostic_cannot_claim_with_old_command_or_old_run(kernel):
    _, store, diagnostics, _, _ = kernel
    result = request(kernel)
    old = diagnostics.claim(result["task_id"], now=0)
    diagnostics.finish(old, now=0, passed=True, checks={"storage": True})
    result = request(kernel, "second")
    with store.write() as records:
        records.add(
            "command_states",
            {
                "id": "old",
                "run_id": old.run_id,
                "generation": old.generation,
                "state": "running",
                "identity_json": "{}",
            },
        )
    with pytest.raises(PermissionError):
        diagnostics.claim(result["task_id"], now=0)


def test_replay_conflict_actor_parameters_and_expiry(kernel):
    request(kernel)
    with pytest.raises(ConflictError):
        request(kernel, duration=2)
    with pytest.raises(PermissionError):
        request(kernel, "agent", actor="agent")
    with pytest.raises(ValueError):
        kernel[2].request(
            "key",
            "provider_connection",
            {"api_key": "never-store-me"},
            LIMITS,
            expected_revision=1,
            now=0,
        )
    expired = request(kernel, "expired", duration=1)
    with pytest.raises(PermissionError):
        kernel[2].claim(expired["task_id"], now=1)


def test_cancel_prevents_model_start_and_cannot_reenable_as_business_task(kernel):
    _, store, diagnostics, _, budgets = kernel
    result = request(kernel, kind="provider_connection")
    task_id = result["task_id"]
    token = diagnostics.claim(task_id, now=0)
    reservation = budgets.reserve(
        token,
        "call",
        BudgetAmounts(active_ms=1000, model_calls=1, tokens=10),
        purpose="model",
        now=0,
    )
    with store.read() as records:
        revision = records.one("tasks", id=task_id)["revision"]
    ControlService(store).cancel("cancel", task_id, expected_revision=revision, now=0)
    with pytest.raises(BudgetDenied):
        budgets.start(token, reservation.reservation_id, now=0)
    budgets.cancel_unstarted(token, reservation.reservation_id, now=0)
    diagnostics.finish(token, now=0, passed=False, checks={"connection": False})
    with store.read() as records:
        task = records.one("tasks", id=task_id)
        assert task["state"] == "cancelled"
    with pytest.raises(PermissionError):
        TaskRecords(store).operate(
            "reenable",
            task_id,
            expected_revision=task["revision"],
            action="reenable",
            now=1,
        )
