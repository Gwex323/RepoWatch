"""SDK review evidence gates and explicit recovery; no live models or publication."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import ModelResponse, ModelSettings, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from test_repairs import (
    RepairModel,
    confirm,
    enable,
    service,  # noqa: F401
)
from test_repairs import (
    pending as repair_pending,
)

from repowatch.repair_model import RepairStageModel, run_repair
from repowatch.store import Conflict


@pytest.fixture
def pending(service):  # noqa: F811
    return repair_pending.__wrapped__(service)


class EarlyReview(RepairModel):
    def __init__(self, *, persistent=False, approved=True, before_read=None):
        super().__init__()
        self.persistent, self.approved = persistent, approved
        self.review_requests = []
        self.before_read = before_read

    async def get_response(self, system_instructions, input, **kwargs):
        reviewing = any(t.name == "read_repair_verification" for t in kwargs["tools"])
        if not reviewing and not self.review_requests:
            return await super().get_response(system_instructions, input, **kwargs)
        self.review_requests.append((system_instructions, input, kwargs))
        count = len(self.review_requests)
        if count == 2 and not self.persistent:
            if self.before_read:
                self.before_read()
            output = ResponseFunctionToolCall(
                id="evidence", call_id="evidence", type="function_call",
                name="read_repair_verification", arguments="{}",
            )
        else:
            output = ResponseOutputMessage(
                id=f"early_{count}", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(
                    type="output_text", annotations=[], text=json.dumps({
                        "approved": self.approved, "summary": "Synthetic review only.",
                        "risks": [],
                    }),
                )],
            )
        return ModelResponse(output=[output], usage=Usage(requests=1), response_id=None)


def install_model(repairs, model):
    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)
    repairs.engine.repair = repair


@pytest.mark.parametrize("approved", [True, False])
def test_early_review_is_discarded_and_real_evidence_read_precedes_final_review(pending, approved):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = EarlyReview(approved=approved)
    install_model(repairs, model)
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert len(model.review_requests) == 3
    for instructions, _, kwargs in model.review_requests[:2]:
        assert kwargs["model_settings"].tool_choice == "read_repair_verification"
        assert [t.name for t in kwargs["tools"]] == ["read_repair_verification"]
        assert kwargs["output_schema"] is None
        assert "exactly one JSON object" not in instructions
    assert sum(e["kind"] == "review_evidence_retry" for e in result["events"]) == 1
    assert sum(e["kind"] == "tool_call" and e["value"] == "read_repair_verification"
               for e in result["events"]) == 1
    assert result["verification"]["passed"] is True and len(runtime.test_calls) == 7
    assert result["status"] == ("pr_created" if approved else "failed")
    assert bool(publisher.calls) == approved
    if not approved:
        assert result["error"] == "repair_review_rejected"


def test_persistent_early_review_never_counts_as_evidence_or_publishes(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = EarlyReview(persistent=True)
    install_model(repairs, model)
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_review_evidence_required"
    assert result["verification"]["passed"] is True and result["review"] is None
    assert len(model.review_requests) == 2 and not publisher.calls
    assert not any(e["kind"] == "tool_call" and e["value"] == "read_repair_verification"
                   for e in result["events"])


def test_pause_during_evidence_correction_stops_before_tool_and_publication(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = EarlyReview(before_read=lambda: repairs.configure(repairs.settings()["version"], False))
    install_model(repairs, model)
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_paused" and not publisher.calls
    assert result["review"] is None


def old_evidence_failure(pending):
    repairs, _, _, _, _ = pending
    task = confirm(pending)
    enable(repairs)
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET attempt=2 WHERE id=?", (task["id"],))
    repairs.event(task["id"], "retry", "3")
    install_model(repairs, EarlyReview(persistent=True))
    asyncio.run(repairs.tick())
    with repairs.store.connection() as db:
        row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        context = json.loads(row["context"])
        context.pop("review_evidence_policy", None)
        db.execute("UPDATE repair_tasks SET attempt=3,error='repair_handoff_incomplete',context=? "
                   "WHERE id=?", (json.dumps(context), task["id"]))
    return repairs.get(task["id"])


def test_old_exhausted_review_retains_patch_and_revalidates_before_native_handoff(pending):
    repairs, _, _, runtime, publisher = pending
    failed = old_evidence_failure(pending)
    assert failed["review_evidence_recovery_available"] and failed["can_retry"]
    assert not asyncio.run(repairs.tick())  # Deployment cannot silently resume failures.
    calls = len(runtime.test_calls)
    queued = repairs.retry(failed["id"], failed["version"])
    assert queued["patch"] == failed["patch"] and queued["diff"] == failed["diff"]
    assert queued["context"]["resume_verification"]
    assert queued["attempt"] == 3
    assert queued["attempts"][-1]["error"] == "repair_handoff_incomplete"
    # A retained patch permits only the native handoff, evidence read and final review.
    model = RepairModel()
    model.calls = 2
    install_model(repairs, model)
    asyncio.run(repairs.tick())
    result = repairs.get(failed["id"])
    assert result["status"] == "pr_created" and result["attempt"] == 4
    assert model.calls == 5 and len(runtime.test_calls) == calls + 7
    assert result["diff"] == failed["diff"] and len(publisher.calls) == 1
    assert not result["review_evidence_recovery_available"]


@pytest.mark.parametrize("case", [
    "policy", "used", "tests_failed", "missing_patch", "review", "remote",
    "repair_stage", "missing_handoff", "already_read", "different_error", "stale",
    "missing_boundary", "wrong_boundary",
])
def test_review_recovery_cannot_bypass_gates_or_repeat(pending, case):
    repairs, _, _, _, publisher = pending
    task = old_evidence_failure(pending)
    with repairs.store.connection() as db:
        row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        context = json.loads(row["context"])
        if case in ("policy", "used"):
            from repowatch.repair_model import REVIEW_EVIDENCE_POLICY
            context.update({"review_evidence_policy": REVIEW_EVIDENCE_POLICY} if case == "policy"
                           else {"review_evidence_recovery_used": True})
            db.execute("UPDATE repair_tasks SET context=?,attempt=? WHERE id=?",
                       (json.dumps(context), 4 if case == "used" else 3, task["id"]))
        assignments = {
            "tests_failed": ("verification", '{"passed":false}'),
            "missing_patch": ("patch", None), "review": ("review", '{"approved":true}'),
            "remote": ("remote", '{}'), "repair_stage": ("stage", "repair_agent"),
            "different_error": ("error", "repair_review_rejected"),
            "stale": ("revision", "f" * 40),
        }
        if case in assignments:
            column, value = assignments[case]
            db.execute(f"UPDATE repair_tasks SET {column}=? WHERE id=?", (value, task["id"]))
        if case == "missing_handoff":
            db.execute("DELETE FROM repair_events WHERE repair_id=? AND kind='handoff'",
                       (task["id"],))
        if case == "already_read":
            repairs.event(task["id"], "tool_call", "read_repair_verification")
        if case == "missing_boundary":
            db.execute("DELETE FROM repair_events WHERE repair_id=? AND kind='retry'",
                       (task["id"],))
        if case == "wrong_boundary":
            db.execute("UPDATE repair_events SET value='2' WHERE repair_id=? AND kind='retry'",
                       (task["id"],))
    result = repairs.get(task["id"])
    assert not result["can_retry"]
    with pytest.raises(Conflict):
        repairs.retry(result["id"], result["version"])
    assert not publisher.calls


@pytest.mark.parametrize("failure", ["tests", "source", "review"])
def test_resumed_patch_still_requires_source_tests_and_independent_review(pending, failure):
    repairs, _, report, runtime, publisher = pending
    failed = old_evidence_failure(pending)
    repairs.retry(failed["id"], failed["version"])
    model = EarlyReview(persistent=True)
    model.calls = 2
    install_model(repairs, model)
    if failure == "tests":
        runtime.full_after = 1
    if failure == "source":
        source = repairs.execution.directory(report["env_id"]) / "source/app.py"
        source.write_text("print('changed')\n")
    asyncio.run(repairs.tick())
    result = repairs.get(failed["id"])
    assert result["error"] == {
        "tests": "repair_tests_failed", "source": "repair_source_changed",
        "review": "repair_review_evidence_required",
    }[failure]
    assert result["remote"] is None and not publisher.calls
    if failure != "review":
        assert not model.review_requests and model.calls == 2
    else:
        assert result["attempt"] == 4 and not result["can_retry"]
        assert not result["review_evidence_recovery_available"]


def test_evidence_gate_discards_entire_parallel_tool_batch(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = EarlyReview(persistent=True)
    delegate = model.get_response

    async def batch(*args, **kwargs):
        response = await delegate(*args, **kwargs)
        if model.review_requests:
            response.output = [ResponseFunctionToolCall(
                id=f"batch_{i}", call_id=f"batch_{i}", type="function_call", name=name,
                arguments="{}" if i == 0 else '{"path":"app.py"}',
            ) for i, name in enumerate(("read_repair_verification", "read_repair_source"))]
        return response

    model.get_response = batch
    install_model(repairs, model)
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_review_evidence_required" and not publisher.calls
    assert not any(e["kind"] == "tool_call" and e["value"] == "read_repair_verification"
                   for e in result["events"])


def test_review_evidence_gate_does_not_extend_exploration_budget():
    delegate = RepairModel()
    model = RepairStageModel(
        delegate, "independent_reviewer", SimpleNamespace(check=lambda: None),
        lambda *_: None, lambda: {"verification_read": False}, {"timeouts": 0},
    )
    model.turns = 3
    with pytest.raises(Conflict, match="repair_review_evidence_required"):
        asyncio.run(model.get_response("", [], handoffs=[], model_settings=ModelSettings()))
    assert delegate.calls == 0
