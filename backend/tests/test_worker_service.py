from test_control_api import api as api
from test_control_api import mutation

from repowatch.application.recovery import RecoveryService
from repowatch.application.runtime_stages import RuntimeStages
from repowatch.domain.budget import BudgetAmounts
from repowatch.worker.service import Worker


def test_real_worker_consumes_explicit_storage_diagnostic_while_global_queue_stays_paused(api):
    client, headers, services, _ = api
    response = client.post(
        "/api/v1/diagnostics",
        headers=headers,
        json=mutation(
            "diag",
            1,
            kind="storage_probe",
            parameters={},
            limits={
                "active_ms": 10000,
                "model_calls": 1,
                "tokens": 1000,
                "repair_rounds": 1,
                "recovery_rounds": 1,
            },
            duration=20,
        ),
    )
    assert response.status_code == 200
    result = Worker(services).run_once()
    assert result["state"] == "diagnostic_completed" and result["result"]["passed"]
    state = client.get("/api/v1/state").json()
    assert state["control"]["paused"] == 1 and state["diagnostics"][0]["state"] == "completed"
    with services.store.read() as records:
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None


def test_worker_diagnostic_insufficient_budget_ends_without_fake_renewal(api):
    client, headers, services, _ = api
    response = client.post(
        "/api/v1/diagnostics",
        headers=headers,
        json=mutation(
            "small",
            1,
            kind="storage_probe",
            parameters={},
            limits={
                "active_ms": 10,
                "model_calls": 1,
                "tokens": 1,
                "repair_rounds": 1,
                "recovery_rounds": 1,
            },
            duration=20,
        ),
    )
    assert response.status_code == 200
    result = Worker(services).run_once()
    assert result["state"] == "diagnostic_failed"
    with services.store.read() as records:
        assert records.find("budget_renewals") == []
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None


def test_worker_expires_queued_diagnostics_without_creating_a_run(api):
    client, headers, services, _ = api
    client.post(
        "/api/v1/diagnostics",
        headers=headers,
        json=mutation(
            "expired",
            1,
            kind="storage_probe",
            parameters={},
            limits={
                "active_ms": 1000,
                "model_calls": 1,
                "tokens": 1,
                "repair_rounds": 1,
                "recovery_rounds": 1,
            },
            duration=1,
        ),
    )
    services.clock = lambda: 2
    assert Worker(services).run_once()["state"] == "idle"
    with services.store.read() as records:
        assert records.find("runs") == []
        assert records.find("diagnostic_authorizations")[0]["state"] == "failed"


def test_normal_task_cannot_execute_without_verified_workspace_adapter(api):
    _, _, services, _ = api
    repo = services.tasks.register_repository("repo", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", now=0)
    services.budgets.authorize_initial(
        "initial",
        task,
        BudgetAmounts(
            active_ms=10000, model_calls=5, tokens=1000, repair_rounds=2, recovery_rounds=2
        ),
        expected_revision=1,
        reason="test",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    assert Worker(services).run_once()["reason"] == "workspace_adapter_missing"
    with services.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert records.find("budget_reservations") == []


def test_intentional_mid_stage_budget_wait_is_not_a_crash_or_completed_checkpoint(api):
    _, _, services, _ = api
    repo = services.tasks.register_repository("repo", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", now=0)
    services.budgets.authorize_initial(
        "initial",
        task,
        BudgetAmounts(
            active_ms=10000, model_calls=5, tokens=1000, repair_rounds=2, recovery_rounds=2
        ),
        expected_revision=1,
        reason="test",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    stage = RuntimeStages(services.store, services.artifacts).begin(token, "main", "primary", now=0)
    with services.store.write() as records:
        records.change("runtime_stages", {"id": stage["id"]}, {"state": "waiting"})
    services.queue.complete(token, now=0, state="waiting", wait_reason="budget_renewal")
    report = RecoveryService(services.store, services.settings).scan_startup(now=0)[0]
    assert report["state"] == "safe_wait"
    assert not report["facts"]["safe_to_reuse_checkpoint"]
    with services.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 0
