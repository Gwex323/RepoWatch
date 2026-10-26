"""Review rejection correction through the real SDK runner and synthetic Docker only."""

import asyncio
import json

import pytest
import test_execution
import test_repair_feedback
import test_repairs
from agents import ModelResponse, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from test_repair_feedback import tool
from test_repairs import confirm, enable, fixture_patch

from repowatch.repair_model import REVIEW_FEEDBACK_POLICY, repair_failure_code, run_repair
from repowatch.repairs import Repairs
from repowatch.store import Conflict


@pytest.fixture
def workspace(tmp_path):
    return test_repair_feedback.workspace.__wrapped__(tmp_path)


@pytest.fixture
def pending(tmp_path):
    return test_repairs.pending.__wrapped__(test_execution.service.__wrapped__(tmp_path))


class ReviewFeedbackModel:
    def __init__(self, workspace, *, rejections=1, failing_candidates=(), action="correct"):
        self.workspace, self.rejections = workspace, rejections
        self.failing_candidates, self.action = failing_candidates, action
        self.calls = self.submissions = self.reviews = 0
        self.read_revisions, self.feedback = [], []

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        if isinstance(input, list):
            for item in input:
                if isinstance(item, dict) and item.get("role") == "user":
                    try:
                        data = json.loads(item.get("content", ""))
                    except (ValueError, TypeError):
                        continue
                    if "review_feedback" in data:
                        self.feedback.append(data["review_feedback"])
        tools = {t.name for t in kwargs["tools"]}
        if kwargs["handoffs"]:
            return tool(self.calls, kwargs["handoffs"][0].tool_name, {})
        if "submit_and_test_repair" in tools:
            if not self.workspace.reads:
                return tool(self.calls, "read_repair_source", {"path": "app.py"})
            if self.workspace.review and self.action == "handoff":
                return tool(self.calls, "transfer_to_independent_reviewer", {})
            self.submissions += 1
            patch = fixture_patch()
            version = 1 if self.action == "repeat" else self.submissions
            patch["edits"][0]["content"] = f"print('review candidate {version}')\n"
            if self.workspace.review and self.action == "unsafe":
                patch["edits"][0]["path"] = "../outside.py"
            self.workspace.execution.runtime.full_after = int(
                self.submissions in self.failing_candidates)
            return tool(self.calls, "submit_and_test_repair", {"patch": patch})
        if kwargs.get("output_schema") is None and "read_repair_verification" in tools:
            self.read_revisions.append(self.workspace.revision)
            return tool(self.calls, "read_repair_verification", {})
        self.reviews += 1
        return ModelResponse(output=[ResponseOutputMessage(
            id=f"review_{self.reviews}", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", annotations=[], text=json.dumps({
                "approved": self.reviews > self.rejections,
                "summary": "Remove the unrelated change and retain the authorized fix.",
                "risks": ["Unrelated behavior must remain unchanged."],
            }))],
        )], usage=Usage(requests=1), response_id=None)


@pytest.mark.parametrize("rejections", [1, 2, 3])
def test_rejection_revises_retests_and_rereads_current_evidence(workspace, rejections):
    model, events = ReviewFeedbackModel(workspace, rejections=rejections), []
    result = asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                                   lambda *e: events.append(e), workspace=workspace))
    rounds = min(rejections + 1, 3)
    assert result["approved"] is (rejections < 3)
    assert model.read_revisions == list(range(1, rounds + 1))
    assert len(workspace.review_history) == len(workspace.verification_history) == rounds
    assert len(workspace.execution.runtime.test_calls) == 7 * rounds
    assert sum(k == "review_feedback" for k, _ in events) == min(rejections, 2)
    assert len({r["patch_digest"] for r in workspace.review_history}) == rounds
    assert result["handoffs"] == ["repair_agent -> independent_reviewer"]
    assert (workspace.original / "app.py").read_bytes() == b"print('ok')"
    if rounds > 1:
        assert model.feedback and model.feedback[0]["review"]["approved"] is False
        assert model.feedback[0]["verification"]["full_suite_after"]["exit_code"] == 0


def test_test_failures_and_review_rejections_share_correction_limits(workspace):
    model = ReviewFeedbackModel(workspace, rejections=2, failing_candidates=(2, 4))
    result = asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                                   lambda *_: None, workspace=workspace))
    assert result["approved"]
    assert model.submissions == 5 and model.read_revisions == [1, 3, 5]
    assert [r["verification"]["passed"] for r in workspace.verification_history] == [
        True, False, True, False, True,
    ]


@pytest.mark.parametrize("action,code", [("repeat", "repair_review_rejected"),
                                        ("unsafe", "repair_unsafe_path"),
                                        ("handoff", "repair_verification_required")])
def test_review_feedback_cannot_repeat_or_bypass_gates(workspace, action, code):
    model = ReviewFeedbackModel(workspace, action=action)
    with pytest.raises(Exception) as exc:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                               lambda *_: None, workspace=workspace))
    assert repair_failure_code(exc.value) == code
    assert len(workspace.verification_history) == len(workspace.review_history) == 1
    assert len(workspace.execution.runtime.test_calls) == 7


@pytest.mark.parametrize("code", ["repair_paused", "repair_interrupted",
                                 "repair_deadline_exceeded", "repair_stale"])
def test_stop_after_rejection_prevents_the_next_model_call(workspace, code):
    stopped = False

    def event(kind, value):
        nonlocal stopped
        stopped = stopped or kind == "review_feedback"

    def check():
        if stopped:
            raise Conflict(code)

    workspace.check = check
    model = ReviewFeedbackModel(workspace)
    with pytest.raises(Conflict, match=code):
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}}, event,
                               workspace=workspace))
    assert model.calls == 5 and model.submissions == model.reviews == 1


def test_each_review_is_durable_and_new_candidate_clears_current_review(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    observed = []

    async def repair(context, event, *, workspace):
        def record(kind, value):
            event(kind, value)
            if kind in ("review_feedback", "patch"):
                observed.append((kind, repairs.get(task["id"])))
        return await run_repair(ReviewFeedbackModel(workspace), context, record,
                                workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    rejected = next(row for kind, row in observed if kind == "review_feedback")
    assert rejected["stage"] == "repair_agent" and rejected["review"]["approved"] is False
    assert len(rejected["review_rounds"]) == 1
    changed = [row for kind, row in observed if kind == "patch"][-1]
    assert changed["review"] is None and changed["verification"] == {}
    result = repairs.get(task["id"])
    assert len(result["review_rounds"]) == 2 and result["status"] == "pr_created"
    assert len(publisher.calls) == 1 and "_reviews" not in result["verification"]


def legacy_rejection(pending):
    repairs, _, _, _, _ = pending
    task = confirm(pending)
    enable(repairs)
    repairs.engine.approved = False
    asyncio.run(repairs.tick())
    with repairs.store.connection() as db:
        row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        context = json.loads(row["context"])
        context.pop("review_feedback_policy")
        db.execute("UPDATE repair_tasks SET attempt=3,context=? WHERE id=?",
                   (json.dumps(context), task["id"]))
    return repairs, task


def test_legacy_rejection_automatically_resumes_once_with_prior_feedback(pending):
    repairs, task = legacy_rejection(pending)
    calls = []

    async def repair(context, event, *, workspace):
        calls.append(context)
        return await run_repair(ReviewFeedbackModel(workspace, rejections=0), context, event,
                                workspace=workspace)

    repairs.engine.repair = repair
    assert repairs.get(task["id"])["review_feedback_recovery_available"]
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created" and result["attempt"] == 4
    assert calls[0]["previous_review"]["review"]["approved"] is False
    assert result["context"]["review_feedback_recovery_used"]
    assert result["context"]["review_feedback_policy"] == REVIEW_FEEDBACK_POLICY
    assert result["attempts"][-1]["error"] == "repair_review_rejected"
    assert not result["review_feedback_recovery_available"]


@pytest.mark.parametrize("change", ["paused", "model_disabled", "stale", "used", "policy"])
def test_legacy_recovery_obeys_authorization_and_policy(pending, change):
    repairs, task = legacy_rejection(pending)
    if change == "paused":
        repairs.configure(repairs.settings()["version"], False)
    elif change == "model_disabled":
        repairs.engine.enabled = False
    else:
        with repairs.store.connection() as db:
            row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
            context = json.loads(row["context"])
            if change == "stale":
                db.execute("UPDATE repair_tasks SET revision='changed' WHERE id=?", (task["id"],))
            elif change == "used":
                context["review_feedback_recovery_used"] = True
            else:
                context["review_feedback_policy"] = REVIEW_FEEDBACK_POLICY
            db.execute("UPDATE repair_tasks SET context=? WHERE id=?",
                       (json.dumps(context), task["id"]))
    before = len(repairs.engine.models)
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "failed"
    assert len(repairs.engine.models) == before


def test_current_policy_rejection_cannot_receive_legacy_allowance(pending):
    repairs, task = legacy_rejection(pending)
    with repairs.store.connection() as db:
        row = dict(db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone())
    assert Repairs._review_feedback_recovery(row)
    context = json.loads(row["context"])
    context["review_feedback_policy"] = REVIEW_FEEDBACK_POLICY
    row["context"] = json.dumps(context)
    assert not Repairs._review_feedback_recovery(row)


def test_repeated_rejections_exhaust_automatic_loop_without_publication(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)

    async def repair(context, event, *, workspace):
        return await run_repair(ReviewFeedbackModel(workspace, rejections=3), context, event,
                                workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_review_rejected" and not publisher.calls
    assert len(result["review_rounds"]) == len(result["verification_rounds"]) == 3
    assert not result["review_feedback_recovery_available"]
    assert not asyncio.run(repairs.tick())


def test_changed_source_stops_legacy_recovery_before_model(pending):
    repairs, task = legacy_rejection(pending)
    with repairs.store.connection() as db:
        row = db.execute("SELECT env_id FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
    source = repairs.execution.directory(row["env_id"]) / "source/app.py"
    source.write_text("print('changed outside the repair')\n", encoding="utf-8")
    calls = len(repairs.engine.models)
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["error"] == "repair_source_changed"
    assert len(repairs.engine.models) == calls


@pytest.mark.parametrize("invalid", ["remote", "approved", "verification", "patch", "digest"])
def test_legacy_allowance_requires_a_valid_unpublished_rejection(invalid):
    row = {
        "context": "{}", "status": "failed", "error": "repair_review_rejected", "attempt": 3,
        "source_digest": "a" * 64, "remote": None, "patch": json.dumps(fixture_patch()),
        "review": json.dumps({"approved": False, "summary": "Revert unrelated edits.",
                              "risks": []}),
        "verification": json.dumps({"passed": True, **{
            key: {"exit_code": 1 if key == "regression_before" else 0}
            for key in ("original_suite", "regression_before", "regression_after",
                        "full_suite_after")
        }}),
    }
    assert Repairs._review_feedback_recovery(row)
    if invalid == "remote":
        row["remote"] = "{}"
    elif invalid == "approved":
        row["review"] = json.dumps({"approved": True, "summary": "Approved", "risks": []})
    elif invalid == "verification":
        row["verification"] = '{"passed":false}'
    elif invalid == "patch":
        row["patch"] = "{}"
    else:
        row["source_digest"] = "not-a-source-digest"
    assert not Repairs._review_feedback_recovery(row)
