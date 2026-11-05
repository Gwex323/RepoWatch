import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256

import pytest

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore, SqlRecords
from repowatch.application.budgets import (
    BudgetedExecutor,
    BudgetService,
    ProgressEvidenceUnavailable,
)
from repowatch.application.orchestration import Orchestrator
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import TaskRecords, block, create_task
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts, BudgetDenied, RenewalPlan
from repowatch.domain.task_records import canonical_json, content_digest, stable_id
from repowatch.operations.migrate import migrate


class Clock:
    def __init__(self):
        self.ms = 0

    def now(self):
        return self.ms / 1000

    def advance(self, ms):
        self.ms += ms


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repo = tasks.register_repository("repo", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, "issue:1", issue_id="issue-1", now=0)
    clock = Clock()
    service = BudgetService(store, settings.artifacts_dir)
    queue.set_pause(False, now=0)
    yield {
        "settings": settings,
        "store": store,
        "tasks": tasks,
        "queue": queue,
        "repo": repo,
        "task": task,
        "clock": clock,
        "budgets": service,
        "executor": BudgetedExecutor(service, queue, clock=clock.now),
    }
    store.close()


def row(k, collection, **identity):
    with k["store"].read() as records:
        return dict(records.one(collection, **identity))


def authorize(k, **changes):
    limits = replace(
        BudgetAmounts(
            active_ms=1_000_000, model_calls=100, tokens=1000, repair_rounds=10, recovery_rounds=10
        ),
        **changes,
    )
    return k["budgets"].authorize_initial(
        "initial-1",
        k["task"],
        limits,
        expected_revision=1,
        reason="夹具用户批准有限初始额度",
        now=k["clock"].now(),
    )


def claim(k):
    return k["queue"].claim(now=k["clock"].now(), ttl=300)


def model(k, token, key, *, tokens=10, upper_tokens=100, elapsed_ms=10):
    def operation():
        k["clock"].advance(elapsed_ms)
        return {"tokens": tokens}

    return k["executor"].execute(
        token,
        key,
        BudgetAmounts(active_ms=max(elapsed_ms, 100), model_calls=1, tokens=upper_tokens),
        operation,
        lambda result: result,
    )


def progress(k, token, *, key="module-a", objective="cover:a", producer="host_verifier"):
    identifier = stable_id("test-evidence", token.run_id, key)
    content = canonical_json(
        {"observed": key, "checks": ["source-read", "result-validated"]}
    ).encode()
    path = k["settings"].artifacts_dir / f"{identifier}.json"
    path.write_bytes(content)
    with k["store"].write() as records:
        snapshot_hash = content_digest(
            json.loads(records.one("runs", id=token.run_id)["snapshot_json"])
        )
        records.add(
            "artifacts",
            {
                "id": identifier,
                "run_id": token.run_id,
                "sha256": sha256(content).hexdigest(),
                "relative_path": path.name,
                "context_json": canonical_json(
                    {
                        "producer": producer,
                        "objective_key": objective,
                        "kind": "coverage",
                        "fact_key": key,
                        "run_snapshot_sha256": snapshot_hash,
                    }
                ),
            },
        )
    fact = k["budgets"].record_verified_progress(
        token,
        objective_key=objective,
        fact_key=key,
        kind="coverage",
        artifact_id=identifier,
        now=k["clock"].now(),
    )
    return fact, identifier


def plan():
    return RenewalPlan(("检查剩余模块并验证",), "沿原目标继续逐模块核验", "完成剩余验证")


def renewal(k, *, used=80, total=100):
    authorize(k, tokens=total)
    token = claim(k)
    model(k, token, "first", tokens=used, upper_tokens=total)
    progress(k, token)
    result = k["budgets"].reserve(
        token,
        "next",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=30),
        purpose="model",
        now=k["clock"].now(),
    )
    assert result.status == "renewal_required"
    request = k["budgets"].request_renewal(
        token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=100)
    )
    return token, request


def decide(k, request, **kwargs):
    return k["budgets"].decide_renewal(
        kwargs.pop("operation_id", "approve-1"),
        request["id"],
        expected_revision=request["revision"],
        decision=kwargs.pop("decision", "approve"),
        reason="用户核对证据后批准",
        now=k["clock"].now(),
        increase=kwargs.pop("increase", BudgetAmounts(tokens=100)),
        **kwargs,
    )


@pytest.mark.parametrize("value", [-1, True, 1.5, float("inf"), 2**53])
def test_budget_amounts_reject_non_finite_negative_or_unsafe_values(value):
    with pytest.raises(ValueError):
        BudgetAmounts(tokens=value)


def test_initial_authorization_is_human_versioned_and_idempotent(kernel):
    k = kernel
    with pytest.raises(PermissionError):
        k["budgets"].authorize_initial(
            "agent",
            k["task"],
            BudgetAmounts(),
            expected_revision=1,
            reason="自批",
            now=0,
            actor="agent",
        )
    first = authorize(k)
    assert authorize(k) == first
    with pytest.raises(ConflictError):
        authorize(k, tokens=2000)
    assert first["limits"]["tokens"] == 1000
    assert first["initial_limits"] == first["limits"]


def test_no_authorization_never_invokes_callback_or_invents_progress(kernel):
    k = kernel
    token = claim(k)
    calls = []
    with pytest.raises(BudgetDenied) as failure:
        k["executor"].execute(
            token,
            "no-budget",
            BudgetAmounts(active_ms=100, model_calls=1, tokens=10),
            lambda: calls.append("called"),
            lambda _: {"tokens": 1},
        )
    assert failure.value.decision.status == "configuration_required"
    assert calls == []
    request = k["budgets"].request_renewal(token, plan(), now=0)
    assert request["report"]["kind"] == "configuration"
    assert request["report"]["progress"] == []
    assert request["report"]["suggested_increase"] is None
    assert request["report"]["plan"]["uncertainty"] == "待核实"
    limits = BudgetAmounts(
        active_ms=10_000, model_calls=10, tokens=100, repair_rounds=3, recovery_rounds=3
    )
    result = decide(k, request, increase=limits)
    assert result["task_state"] == "queued"
    assert result["budget"]["used"]["model_calls"] == 0


def test_reserve_dispatch_actual_and_replay_do_not_double_charge(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    with pytest.raises(PermissionError):
        k["queue"].guard(token, now=0, purpose="model")
    model(k, token, "request-1", tokens=12, upper_tokens=100)
    before = k["budgets"].view(k["task"])
    assert before["used"]["model_calls"] == 1
    assert before["used"]["tokens"] == 12
    assert before["used"]["active_ms"] == 10
    assert not any(before["reserved"].values())
    with pytest.raises(BudgetDenied):
        model(k, token, "request-1", tokens=12)
    assert k["budgets"].view(k["task"])["used"] == before["used"]


def test_failed_sdk_attempt_and_serial_subtask_keep_original_ledger(kernel):
    k = kernel
    authorize(k)
    token = claim(k)

    def failed_attempt():
        k["clock"].advance(5)
        raise RuntimeError("模拟 SDK 瞬时错误")

    with pytest.raises(RuntimeError, match="SDK"):
        k["executor"].execute(
            token,
            "sdk:attempt:1",
            BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
            failed_attempt,
            lambda _: {},
        )
    model(k, token, "sdk:attempt:2", tokens=20)
    model(k, token, "subtask:1:request:1", tokens=30)
    view = k["budgets"].view(k["task"])
    assert view["used"]["model_calls"] == 3
    assert view["used"]["tokens"] == 50
    assert view["reserved"]["tokens"] == 100  # 未知用量不当作零
    assert view["used"]["active_ms"] == 25


def test_unknown_usage_is_persistent_and_can_only_add_verified_receipt_fields(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    result = k["budgets"].reserve(
        token,
        "unknown",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=200),
        purpose="model",
        now=0,
    )
    k["budgets"].start(token, result.reservation_id, now=0)
    k["clock"].advance(10)
    k["budgets"].settle(token, result.reservation_id, {}, now=k["clock"].now())
    restarted = SQLiteBusinessStore(k["settings"])
    service = BudgetService(restarted, k["settings"].artifacts_dir)
    assert service.view(k["task"])["reserved"]["tokens"] == 200
    service.settle(token, result.reservation_id, {"tokens": 50}, now=k["clock"].now())
    replay = service.settle(token, result.reservation_id, {"tokens": 50}, now=k["clock"].now())
    assert replay["used"]["tokens"] == 50
    assert replay["used"]["model_calls"] == 1
    assert replay["reserved"]["tokens"] == 0
    with pytest.raises(ConflictError):
        service.settle(token, result.reservation_id, {"tokens": 0}, now=k["clock"].now())
    restarted.close()


def test_warning_alone_does_not_force_wait_when_next_step_is_funded(kernel):
    k = kernel
    authorize(k, model_calls=10)
    token = claim(k)
    for number in range(8):
        model(k, token, f"call:{number}", tokens=1, upper_tokens=1)
    assert k["budgets"].view(k["task"])["warning"]
    model(k, token, "call:8", tokens=1, upper_tokens=1)
    assert k["budgets"].view(k["task"])["used"]["model_calls"] == 9
    assert row(k, "tasks", id=k["task"])["state"] == "running"
    assert row(k, "global_control", singleton=1)["paused"] == 0


def test_insufficient_next_bound_waits_normally_and_cannot_be_shrunk(kernel):
    k = kernel
    token, request = renewal(k)
    assert row(k, "tasks", id=k["task"])["wait_reason"] == "budget_renewal"
    assert row(k, "execution_lease", singleton=1)["holder_run_id"] is None
    assert row(k, "global_control", singleton=1)["paused"] == 0
    assert request["report"]["budget"]["used"]["tokens"] == 80
    assert request["report"]["completed_goals"] == ["cover:a"]
    assert request["report"]["plan"]["remaining_goals"]
    assert row(k, "runs", id=token.run_id)["state"] == "suspended"
    # 在另一份同账本运行中也不能复用原步骤键缩小上界。
    decide(k, request)
    current = claim(k)
    with pytest.raises(ConflictError):
        k["budgets"].reserve(
            current,
            "next",
            BudgetAmounts(active_ms=100, model_calls=1, tokens=20),
            purpose="model",
            now=k["clock"].now(),
        )


def test_initial_shortfall_has_no_automatic_progress_recommendation(kernel):
    k = kernel
    authorize(k, tokens=10)
    token = claim(k)
    denied = k["budgets"].reserve(
        token,
        "too-large",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=20),
        purpose="model",
        now=0,
    )
    assert denied.status == "renewal_required"
    with pytest.raises(ValueError, match="进展"):
        k["budgets"].request_renewal(
            token, plan(), now=0, suggested_increase=BudgetAmounts(tokens=100)
        )
    request = k["budgets"].request_renewal(token, plan(), now=0)
    assert request["report"]["kind"] == "configuration"
    assert request["report"]["budget"]["used"]["model_calls"] == 0


def test_actual_exhaustion_globally_blocks_and_approval_does_not_resume(kernel):
    k = kernel
    authorize(k, tokens=100)
    token = claim(k)
    model(k, token, "exhaust", tokens=100, upper_tokens=100)
    progress(k, token)
    assert k["budgets"].view(k["task"])["status"] == "exhausted"
    request = k["budgets"].request_renewal(
        token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=100)
    )
    assert row(k, "tasks", id=k["task"])["state"] == "blocked"
    result = decide(k, request)
    assert result["budget"]["limits"]["tokens"] == 200
    assert result["budget"]["used"]["tokens"] == 100
    assert result["task_state"] == "blocked"
    assert result["global_paused"]
    assert k["queue"].claim(now=k["clock"].now()) is None


def test_approval_is_exactly_once_and_preserves_history_and_snapshot(kernel):
    k = kernel
    token, request = renewal(k)
    snapshot = row(k, "runs", id=token.run_id)["snapshot_json"]
    with k["store"].write() as records:
        records.add(
            "progress_attempts",
            {
                "id": "history",
                "task_id": k["task"],
                "run_id": token.run_id,
                "objective_key": "cover:a",
                "fingerprint": "repeated-attempt",
                "evidence_json": "{}",
            },
        )
    first = decide(k, request)
    assert decide(k, request) == first
    assert decide(k, request, operation_id="double-click") == first
    with pytest.raises(ConflictError):
        decide(k, request, operation_id="different-delta", increase=BudgetAmounts(tokens=200))
    assert first["budget"]["limits"]["tokens"] == 200
    assert first["budget"]["initial_limits"]["tokens"] == 100
    assert first["budget"]["used"]["tokens"] == 80
    assert row(k, "runs", id=token.run_id)["snapshot_json"] == snapshot
    assert row(k, "progress_attempts", id="history")["fingerprint"] == "repeated-attempt"
    assert first["task_state"] == "queued"


def test_concurrent_approvals_add_only_one_increment(kernel):
    k = kernel
    _, request = renewal(k)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda key: decide(k, request, operation_id=key), ["click:a", "click:b"])
        )
    assert results[0] == results[1]
    assert k["budgets"].view(k["task"])["limits"]["tokens"] == 200


@pytest.mark.parametrize(
    "changed", ["ledger", "input", "configuration", "acceptance", "code", "progress", "policy"]
)
def test_stale_proposal_rejects_approval_without_adding_budget(kernel, changed):
    k = kernel
    _, request = renewal(k)
    with k["store"].write() as records:
        if changed == "ledger":
            ledger = records.one("budget_ledgers", owner_task_id=k["task"])
            used = json.loads(ledger["used_json"])
            used["tokens"] += 1
            records.change(
                "budget_ledgers",
                {"owner_task_id": k["task"]},
                {"used_json": canonical_json(used), "revision": ledger["revision"] + 1},
            )
        elif changed in {"input", "configuration", "acceptance"}:
            changes = (
                {"input_revision": 3}
                if changed == "input"
                else {"configuration_json": '{"revision":2}'}
                if changed == "configuration"
                else {"acceptance_revision": 2}
            )
            records.change("tasks", {"id": k["task"]}, changes)
        elif changed == "code":
            records.change("repositories", {"id": k["repo"]}, {"target_sha": "a" * 40})
        elif changed == "policy":
            records.change("repositories", {"id": k["repo"]}, {"admission_revision": 2})
        else:
            for artifact in records.find("artifacts"):
                records.change("artifacts", {"id": artifact["id"]}, {"available": 0})
    with pytest.raises(ConflictError):
        decide(k, request)
    assert k["budgets"].view(k["task"])["limits"]["tokens"] == 100


@pytest.mark.parametrize("pause", ["global", "repository", "no_progress"])
def test_approval_keeps_other_pause_and_block_reasons(kernel, pause):
    k = kernel
    _, request = renewal(k)
    if pause == "no_progress":
        with k["store"].write() as records:
            block(records, "no_progress", k["task"], now=k["clock"].now(), window=["a", "b", "a"])
    else:
        k["queue"].set_pause(
            True, now=k["clock"].now(), repository_id=k["repo"] if pause == "repository" else None
        )
    result = decide(k, request)
    assert result["budget"]["limits"]["tokens"] == 200
    assert result["task_state"] == "waiting"
    assert result["wait_reason"] == "resume_required"
    assert k["queue"].claim(now=k["clock"].now()) is None
    if pause == "no_progress":
        with k["store"].read() as records:
            assert records.find("block_reasons", code="no_progress", resolved=0)


@pytest.mark.parametrize(
    ("decision", "expected_state", "wait"),
    [
        ("reject", "waiting", "budget_renewal"),
        ("scope_confirmation", "waiting", "approval"),
        ("cancel", "cancelled", None),
    ],
)
def test_reject_scope_change_and_cancel_have_separate_persistent_results(
    kernel, decision, expected_state, wait
):
    k = kernel
    _, request = renewal(k)
    result = decide(k, request, decision=decision, increase=None)
    assert (result["task_state"], result["wait_reason"]) == (expected_state, wait)
    assert result["budget"]["limits"]["tokens"] == 100


def test_agent_approval_and_zero_or_unbounded_increments_are_rejected(kernel):
    k = kernel
    _, request = renewal(k)
    with pytest.raises(PermissionError):
        decide(k, request, actor="agent")
    with pytest.raises(ValueError):
        decide(k, request, increase=BudgetAmounts())
    assert k["budgets"].view(k["task"])["limits"]["tokens"] == 100


def test_waiting_is_not_billed_and_new_run_does_not_reset_cumulative_use(kernel):
    k = kernel
    _, request = renewal(k)
    before = k["budgets"].view(k["task"])["used"]
    k["clock"].advance(1_000_000)
    decide(k, request)
    current = claim(k)
    model(k, current, "amend:request:1", tokens=5, upper_tokens=30)
    after = k["budgets"].view(k["task"])["used"]
    assert after["active_ms"] == before["active_ms"] + 10
    assert after["tokens"] == before["tokens"] + 5
    assert after["model_calls"] == before["model_calls"] + 1


def test_unknown_usage_cannot_be_avoided_by_releasing_or_moving_a_live_intent(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    reservation = k["budgets"].reserve(
        token,
        "in-flight",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    with pytest.raises(PermissionError):
        k["queue"].complete(token, now=0)
    k["budgets"].start(token, reservation.reservation_id, now=0)
    with pytest.raises(PermissionError):
        k["budgets"].cancel_unstarted(token, reservation.reservation_id, now=0)
    with pytest.raises(PermissionError):
        k["queue"].complete(token, now=0)
    with pytest.raises(ConflictError):
        with k["store"].write() as records:
            records.change("tasks", {"id": k["task"]}, {"budget_owner_task_id": None})


def test_host_time_is_billed_once_across_guard_heartbeat_and_finish(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    reservation = k["budgets"].reserve(
        token,
        "timed",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    k["budgets"].start(token, reservation.reservation_id, now=0)
    k["clock"].advance(10)
    k["queue"].heartbeat(token, now=k["clock"].now())
    k["queue"].guard(
        token, now=k["clock"].now(), purpose="model", reservation_id=reservation.reservation_id
    )
    k["clock"].advance(10)
    k["budgets"].settle(token, reservation.reservation_id, {"tokens": 10}, now=k["clock"].now())
    k["clock"].advance(5)
    k["queue"].complete(token, now=k["clock"].now(), state="waiting", wait_reason="review")
    assert k["budgets"].view(k["task"])["used"]["active_ms"] == 25


def test_overrun_and_actual_usage_beyond_bound_are_charged_and_blocked(kernel):
    k = kernel
    authorize(k, active_ms=100)
    token = claim(k)
    reservation = k["budgets"].reserve(
        token,
        "overrun",
        BudgetAmounts(active_ms=50, model_calls=1, tokens=20),
        purpose="model",
        now=0,
    )
    k["budgets"].start(token, reservation.reservation_id, now=0)
    k["clock"].advance(120)
    k["queue"].heartbeat(token, now=k["clock"].now())
    view = k["budgets"].settle(
        token, reservation.reservation_id, {"tokens": 25}, now=k["clock"].now()
    )
    assert view["used"]["active_ms"] == 120
    assert view["used"]["tokens"] == 25
    assert row(k, "global_control", singleton=1)["paused"] == 1
    k["queue"].complete(token, now=k["clock"].now())
    assert row(k, "tasks", id=k["task"])["state"] == "blocked"


def test_actual_counts_cannot_be_reset_and_progress_proof_must_exist(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    with pytest.raises(ProgressEvidenceUnavailable):
        progress(k, token, key="unverified-module", producer="model_self_report")
    _, artifact_id = progress(k, token)
    path = k["settings"].artifacts_dir / row(k, "artifacts", id=artifact_id)["relative_path"]
    path.write_text("篡改后的报告", encoding="utf-8")
    with pytest.raises(ProgressEvidenceUnavailable):
        k["budgets"].request_renewal(token, plan(), now=0)
    assert row(k, "global_control", singleton=1)["paused"] == 1


def test_approval_checks_artifact_content_again(kernel):
    k = kernel
    _, request = renewal(k)
    with k["store"].read() as records:
        artifact = records.find("artifacts")[0]
    (k["settings"].artifacts_dir / artifact["relative_path"]).unlink()
    with pytest.raises(ProgressEvidenceUnavailable):
        decide(k, request)
    assert k["budgets"].view(k["task"])["limits"]["tokens"] == 100
    assert row(k, "global_control", singleton=1)["paused"] == 1


def test_budget_and_operation_roll_back_if_audit_event_fails(kernel, monkeypatch):
    k = kernel
    _, request = renewal(k)
    original = SqlRecords.add

    def fail(self, collection, values):
        if collection == "business_events" and values["kind"] == "budget_renewal_decided":
            raise StorageError("模拟审计落盘失败")
        return original(self, collection, values)

    monkeypatch.setattr(SqlRecords, "add", fail)
    with pytest.raises(StorageError):
        decide(k, request)
    assert k["budgets"].view(k["task"])["limits"]["tokens"] == 100
    with k["store"].read() as records:
        assert records.one("user_operations", id="approve-1") is None
        assert records.one("budget_renewals", id=request["id"])["state"] == "pending"


def test_shared_scan_uses_original_owner_without_copying_or_rebilling(kernel):
    k = kernel
    authorize(k)
    with k["store"].write() as records:
        scan = create_task(records, k["repo"], "scan:1", None, "scan", 0, owner=k["task"])
        records.change("tasks", {"id": k["task"]}, {"state": "waiting", "wait_reason": "scan"})
    token = claim(k)
    assert row(k, "runs", id=token.run_id)["task_id"] == scan
    model(k, token, "scan:chunk:1", tokens=11)
    assert k["budgets"].view(k["task"])["used"]["tokens"] == 11
    with k["store"].read() as records:
        assert len(records.find("budget_ledgers")) == 1
    k["queue"].complete(token, now=k["clock"].now())
    assert k["budgets"].view(k["task"])["used"]["tokens"] == 11


def test_repair_and_recovery_rounds_are_stable_across_new_runs(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    for key, amount in (
        ("repair:1", BudgetAmounts(repair_rounds=1)),
        ("recovery:1", BudgetAmounts(recovery_rounds=1)),
    ):
        reservation = k["budgets"].reserve(token, key, amount, purpose="counter", now=0)
        k["budgets"].start(token, reservation.reservation_id, now=0)
        k["budgets"].settle(token, reservation.reservation_id, {}, now=0)
    k["queue"].complete(token, now=0, state="queued")
    current = claim(k)
    denied = k["budgets"].reserve(
        current, "repair:1", BudgetAmounts(repair_rounds=1), purpose="counter", now=0
    )
    assert denied.status == "already_recorded"
    used = k["budgets"].view(k["task"])["used"]
    assert (used["repair_rounds"], used["recovery_rounds"]) == (1, 1)


def test_request_replay_after_safe_release_returns_same_persisted_proposal(kernel):
    k = kernel
    token, request = renewal(k)
    repeated = k["budgets"].request_renewal(
        token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=100)
    )
    assert repeated == request
    with k["store"].read() as records:
        assert len(records.find("budget_renewals", owner_task_id=k["task"])) == 1
    with pytest.raises(ConflictError):
        k["budgets"].request_renewal(
            token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=101)
        )


def test_concurrent_requests_share_one_proposal_and_do_not_repeat_release(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    progress(k, token)

    def request(_):
        return k["budgets"].request_renewal(
            token, plan(), now=0, suggested_increase=BudgetAmounts(tokens=10)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, [1, 2]))
    assert results[0] == results[1]
    with k["store"].read() as records:
        assert len(records.find("budget_renewals", state="pending")) == 1
        assert len(records.find("business_events", kind="run_finished")) == 1


def test_repeated_approval_reads_original_result_even_after_evidence_is_removed(kernel):
    k = kernel
    _, request = renewal(k)
    first = decide(k, request)
    with k["store"].read() as records:
        artifact = records.find("artifacts")[0]
    (k["settings"].artifacts_dir / artifact["relative_path"]).unlink()
    assert decide(k, request, operation_id="retry-after-approval") == first
    assert row(k, "global_control", singleton=1)["paused"] == 0


@pytest.mark.parametrize("receipt", [None, {"tokens": None}, {"model_calls": 0}, {"active_ms": 0}])
def test_invalid_sdk_receipt_keeps_unknown_usage_reserved(kernel, receipt):
    k = kernel
    authorize(k)
    token = claim(k)
    result = k["executor"].execute(
        token,
        "invalid-receipt",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
        lambda: "response",
        lambda _: receipt,
    )
    assert result == "response"
    view = k["budgets"].view(k["task"])
    assert view["used"]["model_calls"] == 1
    assert view["reserved"]["tokens"] == 100


def test_unstarted_intent_can_be_released_but_never_becomes_a_model_call(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    pending = k["budgets"].reserve(
        token,
        "not-sent",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    k["budgets"].cancel_unstarted(token, pending.reservation_id, now=0)
    k["budgets"].cancel_unstarted(token, pending.reservation_id, now=0)
    k["queue"].complete(token, now=0)
    view = k["budgets"].view(k["task"])
    assert view["used"]["model_calls"] == 0
    assert not any(view["reserved"].values())


def test_database_rejects_budget_reset_and_guard_rejects_another_purpose(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    pending = k["budgets"].reserve(
        token,
        "specific-purpose",
        BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    k["budgets"].start(token, pending.reservation_id, now=0)
    with pytest.raises(PermissionError):
        k["queue"].guard(token, now=0, purpose="publish", reservation_id=pending.reservation_id)
    with pytest.raises(ValueError):
        k["budgets"].settle(token, pending.reservation_id, {"model_calls": 0, "tokens": 10}, now=0)
    k["budgets"].settle(token, pending.reservation_id, {"tokens": 10}, now=0)
    with pytest.raises(ConflictError):
        with k["store"].write() as records:
            records.change("budget_ledgers", {"owner_task_id": k["task"]}, {"used_json": "{}"})
    assert k["budgets"].view(k["task"])["used"]["model_calls"] == 1


def test_shared_scan_renewal_credits_fixed_owner_and_keeps_parent_dependency(kernel):
    k = kernel
    authorize(k, tokens=100)
    with k["store"].write() as records:
        scan = create_task(records, k["repo"], "scan:renewal", None, "scan", 0, owner=k["task"])
        records.change("tasks", {"id": k["task"]}, {"state": "waiting", "wait_reason": "scan"})
        records.add("scan_dependencies", {"waiting_task_id": k["task"], "scan_task_id": scan})
    token = claim(k)
    model(k, token, "scan:work", tokens=80, upper_tokens=90)
    progress(k, token)
    request = k["budgets"].request_renewal(
        token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=100)
    )
    assert request["owner_task_id"] == k["task"]
    assert request["task_id"] == scan
    result = decide(k, request)
    assert result["budget"]["used"]["tokens"] == 80
    assert result["budget"]["limits"]["tokens"] == 200
    assert row(k, "tasks", id=k["task"])["wait_reason"] == "scan"
    with k["store"].read() as records:
        assert len(records.find("budget_ledgers")) == 1
        assert records.find("scan_dependencies", scan_task_id=scan)


def test_already_approved_progress_is_not_reused_as_new_renewal_progress(kernel):
    k = kernel
    _, request = renewal(k)
    approved = decide(k, request)
    assert approved["budget"]["approved_progress_ids"]
    token = claim(k)
    model(k, token, "more-work", tokens=110, upper_tokens=120)
    with pytest.raises(ValueError, match="新进展"):
        k["budgets"].request_renewal(
            token, plan(), now=k["clock"].now(), suggested_increase=BudgetAmounts(tokens=100)
        )
    manual = k["budgets"].request_renewal(token, plan(), now=k["clock"].now())
    assert manual["report"]["progress"]
    assert manual["report"]["recent_progress"] == []
    assert manual["report"]["kind"] == "configuration"
    assert manual["report"]["suggested_increase"] is None


@pytest.mark.parametrize("changed", ["code", "configuration", "acceptance"])
def test_new_request_cannot_silently_adopt_a_different_execution_scope(kernel, changed):
    k = kernel
    authorize(k)
    token = claim(k)
    progress(k, token)
    with k["store"].write() as records:
        if changed == "code":
            records.change("repositories", {"id": k["repo"]}, {"target_sha": "b" * 40})
        else:
            values = (
                {"configuration_json": '{"revision":2}'}
                if changed == "configuration"
                else {"acceptance_revision": 2}
            )
            records.change("tasks", {"id": k["task"]}, values)
    with pytest.raises(ConflictError):
        k["budgets"].request_renewal(
            token, plan(), now=0, suggested_increase=BudgetAmounts(tokens=100)
        )
    with k["store"].read() as records:
        assert records.find("budget_renewals") == []


def test_renewal_preserves_independent_run_safety_limits(kernel):
    k = kernel
    authorize(k)
    policy = {"max_steps": 3, "model_timeout_ms": 100, "max_output_tokens": 30}
    token = k["queue"].claim(now=0, snapshot={"safety_policy": policy})
    progress(k, token)
    request = k["budgets"].request_renewal(
        token, plan(), now=0, suggested_increase=BudgetAmounts(tokens=100)
    )
    decide(k, request)
    assert json.loads(row(k, "runs", id=token.run_id)["snapshot_json"])["safety_policy"] == policy


def test_real_sdk_explicit_attempts_are_separately_reserved(kernel):
    import httpx
    from openai import APIStatusError, OpenAI

    k = kernel
    authorize(k)
    token = claim(k)
    requests = []

    def transport(request):
        requests.append(request.url.path)
        k["clock"].advance(5)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "fixture retry"}})
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            },
        )

    with OpenAI(
        api_key="fixture-unused",
        base_url="https://budget-fixture.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    ) as client:

        def invoke():
            return client.chat.completions.create(
                model="fixture", messages=[{"role": "user", "content": "fixture"}]
            )

        bound = BudgetAmounts(active_ms=100, model_calls=1, tokens=100)
        with pytest.raises(APIStatusError):
            k["executor"].execute(
                token,
                "sdk:try:1",
                bound,
                invoke,
                lambda response: {"tokens": response.usage.total_tokens},
            )
        k["executor"].execute(
            token,
            "sdk:try:2",
            bound,
            invoke,
            lambda response: {"tokens": response.usage.total_tokens},
        )
    assert len(requests) == 2
    view = k["budgets"].view(k["task"])
    assert view["used"]["model_calls"] == 2
    assert view["used"]["tokens"] == 10
    assert view["reserved"]["tokens"] == 100


@pytest.mark.parametrize(
    "event_kind", ["budget_reserved", "budget_step_started", "budget_usage_recorded"]
)
def test_executor_never_returns_success_when_budget_persistence_fails(
    kernel, monkeypatch, event_kind
):
    k = kernel
    authorize(k)
    token = claim(k)
    original = SqlRecords.add
    calls = []

    def fail(self, collection, values):
        if collection == "business_events" and values["kind"] == event_kind:
            raise StorageError("模拟预算持久化失败")
        return original(self, collection, values)

    monkeypatch.setattr(SqlRecords, "add", fail)

    def operation():
        calls.append("executed")
        return {"tokens": 10}

    with pytest.raises(StorageError):
        k["executor"].execute(
            token,
            "persist-failure",
            BudgetAmounts(active_ms=100, model_calls=1, tokens=100),
            operation,
            lambda value: value,
        )
    assert len(calls) == (1 if event_kind == "budget_usage_recorded" else 0)
    if event_kind != "budget_reserved":
        assert k["budgets"].view(k["task"])["reserved"]["model_calls"] == 1
        with pytest.raises(PermissionError):
            k["queue"].complete(token, now=0)
    assert k["budgets"].view(k["task"])["used"]["model_calls"] == 0


def test_invalid_progress_context_fails_closed_before_request(kernel):
    k = kernel
    authorize(k)
    token = claim(k)
    _, artifact = progress(k, token)
    with k["store"].write() as records:
        # Seed an invalid legacy record; current immutable evidence cannot be edited.
        legacy = dict(records.one("artifacts", id=artifact))
        legacy.update(id="legacy-invalid", relative_path="legacy-invalid.json", context_json="[]")
        records.add("artifacts", legacy)
        fact = dict(records.find("budget_progress")[0])
        fact.update(id="legacy-fact", fact_key="legacy", artifact_id=legacy["id"])
        records.add("budget_progress", fact)
    with pytest.raises(ProgressEvidenceUnavailable):
        k["budgets"].request_renewal(
            token, plan(), now=0, suggested_increase=BudgetAmounts(tokens=100)
        )
    assert row(k, "global_control", singleton=1)["paused"] == 1


def test_stale_usage_proposal_can_be_refreshed_without_starting_a_run(kernel):
    k = kernel
    _, request = renewal(k)
    with k["store"].write() as records:
        ledger = records.one("budget_ledgers", owner_task_id=k["task"])
        used = json.loads(ledger["used_json"])
        used["tokens"] += 1
        records.change(
            "budget_ledgers",
            {"owner_task_id": k["task"]},
            {
                "used_json": canonical_json(used),
                "revision": ledger["revision"] + 1,
            },
        )
    with pytest.raises(ConflictError):
        decide(k, request)
    refreshed = k["budgets"].refresh_renewal(
        "refresh-1",
        request["id"],
        expected_revision=1,
        reason="核对晚到用量后刷新",
        now=k["clock"].now(),
        suggested_increase=BudgetAmounts(tokens=100),
    )
    assert refreshed["revision"] == 2
    assert refreshed["report"]["budget"]["used"]["tokens"] == 81
    assert (
        k["budgets"].refresh_renewal(
            "refresh-1",
            request["id"],
            expected_revision=1,
            reason="核对晚到用量后刷新",
            now=k["clock"].now(),
            suggested_increase=BudgetAmounts(tokens=100),
        )
        == refreshed
    )
    result = decide(k, refreshed)
    assert result["budget"]["limits"]["tokens"] == 200
    assert result["budget"]["used"]["tokens"] == 81
    with k["store"].read() as records:
        old = records.one("budget_renewal_revisions", renewal_id=request["id"], revision=1)
        assert json.loads(old["report_json"])["budget"]["used"]["tokens"] == 80
        assert len(records.find("runs")) == 1


@pytest.mark.parametrize("changed", ["input", "configuration", "code"])
def test_refreshed_scope_requires_revalidation_and_approval_does_not_start_it(kernel, changed):
    k = kernel
    _, request = renewal(k)
    with k["store"].write() as records:
        if changed == "input":
            records.change("tasks", {"id": k["task"]}, {"input_revision": 5})
        elif changed == "configuration":
            records.change("tasks", {"id": k["task"]}, {"configuration_json": '{"revision":2}'})
        else:
            records.change("repositories", {"id": k["repo"]}, {"target_sha": "c" * 40})
    refreshed = k["budgets"].refresh_renewal(
        "scope-refresh",
        request["id"],
        expected_revision=1,
        reason="核对新对象后刷新预算资料，执行条件仍待重核",
        now=k["clock"].now(),
    )
    assert refreshed["report"]["requires_revalidation"]
    result = decide(k, refreshed)
    assert result["task_state"] == "waiting"
    assert result["wait_reason"] == "resume_required"
    assert result["budget"]["limits"]["tokens"] == 200
    assert row(k, "execution_lease", singleton=1)["holder_run_id"] is None


def test_reconsidering_rejected_request_is_an_explicit_human_operation(kernel):
    k = kernel
    _, request = renewal(k)
    decide(k, request, decision="reject", increase=None)
    with pytest.raises(PermissionError):
        k["budgets"].refresh_renewal(
            "agent-refresh",
            request["id"],
            expected_revision=1,
            reason="自动重试申请",
            now=k["clock"].now(),
            actor="agent",
        )
    refreshed = k["budgets"].refresh_renewal(
        "human-refresh",
        request["id"],
        expected_revision=1,
        reason="用户重新核对后决定调整额度",
        now=k["clock"].now(),
    )
    assert refreshed["revision"] == 2
    result = decide(k, refreshed, operation_id="approve-reconsidered")
    assert result["budget"]["limits"]["tokens"] == 200


def test_refresh_audit_failure_keeps_original_proposal_revision(kernel, monkeypatch):
    k = kernel
    _, request = renewal(k)
    original = SqlRecords.add

    def fail(self, collection, values):
        if collection == "business_events" and values["kind"] == "budget_renewal_refreshed":
            raise StorageError("模拟刷新审计失败")
        return original(self, collection, values)

    monkeypatch.setattr(SqlRecords, "add", fail)
    with pytest.raises(StorageError):
        k["budgets"].refresh_renewal(
            "refresh-failure",
            request["id"],
            expected_revision=1,
            reason="用户刷新",
            now=k["clock"].now(),
        )
    assert row(k, "budget_renewals", id=request["id"])["revision"] == 1
    with k["store"].read() as records:
        assert records.find("budget_renewal_revisions") == []
        assert records.one("user_operations", id="refresh-failure") is None


@pytest.mark.parametrize("grant_path", ["initial", "configuration_request"])
def test_grant_below_migrated_usage_preserves_usage_and_immediately_blocks(kernel, grant_path):
    k = kernel
    with k["store"].write() as records:
        records.change(
            "budget_ledgers", {"owner_task_id": k["task"]}, {"used_json": '{"tokens":21}'}
        )
    limits = BudgetAmounts(
        active_ms=10000, model_calls=10, tokens=10, repair_rounds=3, recovery_rounds=3
    )
    if grant_path == "initial":
        view = k["budgets"].authorize_initial(
            "initial-migrated",
            k["task"],
            limits,
            expected_revision=1,
            reason="核对迁移前用量后设置额度",
            now=0,
        )
    else:
        token = claim(k)
        request = k["budgets"].request_renewal(token, plan(), now=0)
        result = decide(k, request, increase=limits)
        view = result["budget"]
        assert result["task_state"] == "blocked"
    assert view["used"]["tokens"] == 21
    assert view["status"] == "exhausted"
    assert row(k, "global_control", singleton=1)["paused"] == 1
    assert k["queue"].claim(now=0) is None
