import json

import pytest
from test_roles import bind, task_run
from test_roles import kernel as kernel

from repowatch.application.control import ControlService
from repowatch.application.delegation import Delegation
from repowatch.application.progress import ProgressService
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import ExecutionToken, canonical_json, content_digest


def setup(k, *, policy=None):
    if policy:
        # The fixture approves configuration before taking the run snapshot.
        original_claim = k.queue.claim

        def configured_claim(**kwargs):
            with k.store.write() as records:
                task = records.find("tasks", mode="triage")[0]
                records.change(
                    "tasks",
                    {"id": task["id"]},
                    {"configuration_json": canonical_json({"runtime_policy": policy})},
                )
            return original_claim(**kwargs)

        k.queue.claim = configured_claim
    task, token = task_run(k)
    return task, token, ProgressService(k.store, k.artifacts, k.budgets)


def observation(
    k, token, key, *, semantic="same", facts=(), status="completed", producer="host_stage_verifier"
):
    return k.artifacts.register(
        token,
        key,
        canonical_json(
            {
                "producer": producer,
                "objective_key": "primary",
                "status": status,
                "comparison": {
                    "action": "verify",
                    "semantic_state": semantic,
                    "timestamp": key,
                    "target_sha": key,
                },
                "verified_fact_ids": list(facts),
            }
        ).encode(),
        context={"producer": producer, "objective_key": "primary"},
        now=0,
    )


def fact(k, token, key="module-a", kind="coverage"):
    with k.store.read() as records:
        snapshot_hash = content_digest(
            json.loads(records.one("runs", id=token.run_id)["snapshot_json"])
        )
    artifact = k.artifacts.register(
        token,
        "proof:" + key,
        canonical_json({"actual_fixture_check": key}).encode(),
        context={
            "producer": "host_verifier",
            "objective_key": "primary",
            "kind": kind,
            "fact_key": key,
            "run_snapshot_sha256": snapshot_hash,
        },
        now=0,
    )
    return k.budgets.record_verified_progress(
        token, objective_key="primary", fact_key=key, kind=kind, artifact_id=artifact, now=0
    )


def test_three_completed_empty_attempts_ignore_timestamps_and_pause(kernel):
    k = kernel
    _, token, progress = setup(k)
    for i in range(3):
        result = progress.record(token, "primary", observation(k, token, str(i)), now=0)
    assert result["blocked"] == "no_progress"
    with k.store.read() as records:
        rows = records.find("progress_attempts")
        assert len({row["fingerprint"] for row in rows}) == 1
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_real_new_coverage_advances_once_and_run_or_sha_does_not_reset_history(kernel):
    k = kernel
    task, token, progress = setup(k)
    for i in range(2):
        progress.record(token, "primary", observation(k, token, str(i)), now=0)
    verified = fact(k, token)
    result = progress.record(
        token, "primary", observation(k, token, "new", facts=(verified,)), now=0
    )
    assert result["progress"] and result["blocked"] is None
    progress.record(token, "primary", observation(k, token, "repeat", facts=(verified,)), now=0)
    k.queue.complete(token, now=0, state="queued")
    with k.store.write() as records:
        repo = records.one("tasks", id=task)["repository_id"]
        records.change("repositories", {"id": repo}, {"target_sha": "b" * 40})
    next_token = k.queue.claim(now=0)
    progress.record(next_token, "primary", observation(k, next_token, "new-run-1"), now=0)
    result = progress.record(next_token, "primary", observation(k, next_token, "new-run-2"), now=0)
    assert result["blocked"] == "no_progress"
    with k.store.read() as records:
        assert records.find("progress_windows")[0]["progress_revision"] == 1
        assert len(records.find("progress_attempts")) == 6


def test_two_oscillation_cycles_without_new_evidence_are_detected(kernel):
    k = kernel
    _, token, progress = setup(k, policy={"no_progress_attempts": 10})
    for i, state in enumerate(("A", "B", "A", "B", "A")):
        result = progress.record(
            token, "primary", observation(k, token, str(i), semantic=state), now=0
        )
    assert result["blocked"] == "oscillation"


def test_normal_wait_or_running_check_never_counts_as_failure(kernel):
    k = kernel
    _, token, progress = setup(k)
    for i in range(5):
        result = progress.record(
            token,
            "primary",
            observation(k, token, str(i), status="running" if i % 2 else "waiting"),
            now=0,
        )
        assert not result["recorded"]
    with k.store.read() as records:
        assert records.find("progress_attempts") == []


def test_agent_report_or_nonexistent_progress_fact_is_rejected(kernel):
    k = kernel
    _, token, progress = setup(k)
    with pytest.raises(PermissionError):
        progress.record(token, "primary", observation(k, token, "agent", producer="agent"), now=0)
    with pytest.raises(PermissionError):
        progress.record(
            token, "primary", observation(k, token, "invalid", facts=("invented",)), now=0
        )


def test_explicit_bounded_retry_starts_new_window_and_preserves_old_attempts(kernel):
    k = kernel
    task, token, progress = setup(k)
    for i in range(3):
        progress.record(token, "primary", observation(k, token, str(i)), now=0)
    k.queue.complete(token, now=0, state="blocked", outcome="no_progress")
    control = ControlService(k.store)
    with k.store.read() as records:
        reason = dict(records.find("block_reasons", code="no_progress")[0])
    control.resolve_reason(
        "resolve",
        reason["id"],
        expected_revision=reason["revision"],
        now=0,
        explanation="调整策略并核对新增材料",
        retry_plan={"steps": ["执行不同验证"], "success_condition": "产生新的可核验证据"},
        retry_limits={"max_attempts": 1, "max_steps": 5, "max_active_ms": 5000},
    )
    with k.store.read() as records:
        revision = records.one("global_control", singleton=1)["revision"]
    control.resume("resume", expected_revision=revision, now=0)
    retry = k.queue.claim(now=0)
    result = progress.record(retry, "primary", observation(k, retry, "retry"), now=0)
    assert result["blocked"] == "bounded_retry_exhausted"
    with k.store.read() as records:
        assert len(records.find("progress_attempts")) == 4
        assert records.find("progress_windows")[0]["window"] == 1


def test_delegated_child_shares_one_clock_and_parent_execution_is_suspended(kernel):
    k = kernel
    bind(k, "subtask.investigator")
    task, parent = task_run(k)
    child = Delegation(k.store).start(
        parent, "child-1", mode="investigate", role_key="subtask.investigator", now=0
    )
    with pytest.raises(PermissionError):
        k.queue.complete(parent, now=0)
    assert not k.budgets.reserve(
        parent,
        "parent",
        BudgetAmounts(active_ms=1000, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    ).allowed
    with pytest.raises(PermissionError):
        Delegation(k.store).start(
            parent, "child-2", mode="investigate", role_key="subtask.investigator", now=0
        )
    with pytest.raises(PermissionError):
        k.queue.guard(
            ExecutionToken(child.run_id, child.generation, "forged"), now=0, purpose="result"
        )
    decision = k.budgets.reserve(
        child,
        "child-model",
        BudgetAmounts(active_ms=5000, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    k.budgets.start(child, decision.reservation_id, now=0)
    k.budgets.settle(child, decision.reservation_id, {"model_calls": 1, "tokens": 5}, now=2)
    k.queue.heartbeat(parent, now=3)
    k.queue.complete(child, now=3)
    assert k.budgets.view(task)["used"]["active_ms"] == 3000
    with k.store.read() as records:
        assert records.one("execution_lease", singleton=1)["holder_run_id"] == parent.run_id
        assert records.one("tasks", id=task)["state"] == "running"
        assert records.one("runs", id=child.run_id)["state"] == "ended"
    k.queue.complete(parent, now=3)


@pytest.mark.parametrize("kind", ["refutation", "validated_evidence", "verified_subproblem"])
def test_real_new_facts_advance_complex_goal_and_repeated_fact_does_not(kernel, kind):
    k = kernel
    _, token, progress = setup(k)
    for index in range(2):
        progress.record(token, "primary", observation(k, token, str(index)), now=0)
    proof = fact(k, token, kind, kind=kind)
    assert progress.record(
        token, "primary", observation(k, token, "advance", facts=(proof,)), now=0
    )["progress"]
    assert not progress.record(
        token, "primary", observation(k, token, "repeat", facts=(proof,)), now=0
    )["progress"]


def test_process_store_restart_and_finite_budget_addition_keep_detection_history(kernel):
    from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
    from repowatch.application.artifacts import ArtifactService
    from repowatch.application.budget_adjustments import BudgetAdjustments
    from repowatch.application.budgets import BudgetService
    from repowatch.application.orchestration import Orchestrator

    k = kernel
    task, token, progress = setup(k)
    for index in range(2):
        progress.record(token, "primary", observation(k, token, str(index)), now=0)
    k.queue.complete(token, now=0, state="queued")
    k.queue.set_pause(True, now=0)
    adjustments = BudgetAdjustments(k.store)
    basis = adjustments.basis(task)
    adjustments.approve(
        "manual-add",
        task,
        BudgetAmounts(tokens=1000),
        expected_revision=basis["ledger"]["revision"],
        expected_basis=basis["basis_sha256"],
        reason="有限加额不清除检测历史",
        now=0,
    )
    k.store.close()
    k.store = SQLiteBusinessStore(k.settings)
    k.artifacts = ArtifactService(k.store, k.settings.artifacts_dir)
    k.budgets = BudgetService(k.store, k.settings.artifacts_dir)
    k.queue = Orchestrator(k.store)
    progress = ProgressService(k.store, k.artifacts, k.budgets)
    k.queue.set_pause(False, now=0)
    next_token = k.queue.claim(now=0)
    assert (
        progress.record(next_token, "primary", observation(k, next_token, "after-restart"), now=0)[
            "blocked"
        ]
        == "no_progress"
    )


def test_healthy_delegated_worker_startup_is_not_a_crash_and_root_abandon_covers_child(kernel):
    from repowatch.application.recovery import RecoveryService

    k = kernel
    bind(k, "subtask.investigator")
    _, parent = task_run(k)
    child = Delegation(k.store).start(
        parent, "recovery-child", mode="investigate", role_key="subtask.investigator", now=0
    )
    recovery = RecoveryService(k.store, k.settings)
    recovery.scan_startup(now=0)
    with k.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 0
    report = recovery.report(parent.run_id, now=31)
    assert report["facts"]["safe_to_abandon"]
    recovery.abandon(
        "abandon-family",
        report["id"],
        expected_basis=report["basis_sha256"],
        explanation="核对父子运行均无活跃副作用",
        now=31,
    )
    with k.store.read() as records:
        assert records.one("runs", id=child.run_id)["state"] == "abandoned"
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
