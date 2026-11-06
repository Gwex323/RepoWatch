import pytest
from test_control_api import api as api
from test_control_api import mutation

from repowatch.application.recovery import RecoveryService
from repowatch.application.runtime_stages import RuntimeStages
from repowatch.application.storage import ConflictError
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.worker.service import Worker


def test_confirmed_receipt_request_only_reconciles_without_repair_or_scan(api, monkeypatch):
    _, _, services, _ = api
    repository = services.tasks.register_repository("receipt-worker", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repository, "issue:1", issue_id="101", now=0)
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"state": "waiting", "wait_reason": "pr_review"})
        records.add(
            "publication_intents",
            {
                "id": "receipt",
                "task_id": task,
                "repository_id": repository,
                "issue_id": "101",
                "kind": "pull_request",
                "state": "confirmed",
                "request_json": canonical_json({"upstream_id": "receipt-worker"}),
                "request_sha256": "fixture",
                "created_at": 0,
            },
        )
        revision = records.one("tasks", id=task)["revision"]
    services.budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.tasks.operate(
        "reconcile", task, expected_revision=revision, action="reconcile_publication", now=0
    )
    services.queue.set_pause(False, now=0)
    calls = []

    def reconcile(publisher, token, intent):
        calls.append(intent["id"])
        services.queue.complete(token, now=0, state="waiting", wait_reason="pr_review")
        return {"state": "waiting", "reason": "pr_review"}

    monkeypatch.setattr("repowatch.application.publication.Publisher.reconcile_pending", reconcile)
    assert Worker(services).run_once() == {"state": "waiting", "reason": "pr_review"}
    assert calls == ["receipt"]
    with services.store.read() as records:
        assert not records.find("workspaces", task_id=task)
        assert not records.find("budget_reservations")
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None


@pytest.mark.parametrize("failure", ["reproduction", "input_changed", "version_conflict"])
def test_repair_failure_blocks_but_new_input_safely_retriages(api, monkeypatch, failure):
    _, _, services, _ = api
    repository = services.tasks.register_repository("repair-failure", admission_mode="all_bugs")
    task = services.tasks.ensure_task(
        repository, "issue:1", issue_id="101", mode="implement", now=0
    )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "target_sha": "a" * 40,
                "scan_ready_sha": "a" * 40,
            },
        )
    services.budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)

    def rejected(coordinator, token):
        if failure == "reproduction":
            raise PermissionError("未取得有效修改前证明")
        if failure == "input_changed":
            services.tasks.observe(task, "issue:1", "new", {"body": "changed"}, now=0)
        raise ConflictError("version changed")

    monkeypatch.setattr("repowatch.worker.repairs.RepairCoordinator.run", rejected)
    outcome = Worker(services).run_once()
    with services.store.read() as records:
        current = records.one("tasks", id=task)
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
        assert current["state"] == ("queued" if failure == "input_changed" else "blocked")
        assert records.one("global_control", singleton=1)["paused"] == (failure != "input_changed")
        if failure == "input_changed":
            assert current["mode"] == "triage"
            assert outcome["reason"] == "input_changed"


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


def test_worker_triage_other_assignee_waits_without_model_or_global_pause(api):
    _, _, services, _ = api
    repo = services.tasks.register_repository("triage-repo", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:other", issue_id="other", now=0)
    services.tasks.observe(
        task, "issue:other", "v1", {"state": "open", "assignees": ["other"]}, now=0
    )
    with services.store.write() as records:
        records.change(
            "repositories", {"id": repo}, {"identity_json": '{"execution_login":"worker"}'}
        )
    services.budgets.authorize_initial(
        "triage-budget",
        task,
        BudgetAmounts(
            active_ms=10000, model_calls=5, tokens=1000, repair_rounds=2, recovery_rounds=2
        ),
        expected_revision=1,
        reason="test",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    result = Worker(services).run_once()
    assert result == {"state": "ended", "reason": "other_assignee"}
    with services.store.read() as records:
        assert records.one("tasks", id=task)["outcome"] == "policy_excluded"
        assert records.one("global_control", singleton=1)["paused"] == 0
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
        assert not records.find("budget_reservations")
        assert records.find("triage_decisions", task_id=task)[0]["reason_code"] == "other_assignee"


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
