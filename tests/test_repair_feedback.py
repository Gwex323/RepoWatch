"""Native SDK feedback loops with immutable candidates and synthetic execution only."""

import ast
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from agents import ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from test_execution import service  # noqa: F401
from test_repairs import (  # noqa: F401
    RepairModel,
    RepairRuntime,
    confirm,
    enable,
    fixture_patch,
    pending,
)

from repowatch.repair_model import (
    VERIFICATION_POLICY,
    repair_failure_code,
    run_repair,
    verification_feedback,
)
from repowatch.repair_workspace import Patch, RepairWorkspace
from repowatch.repairs import Repairs
from repowatch.store import Conflict


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "environment" / "source"
    (source / "tests").mkdir(parents=True)
    (source / "app.py").write_bytes(b"print('ok')")
    (source / "tests/test_original.py").write_text("assert True\n", encoding="utf-8")
    root = tmp_path / "repairs"
    root.mkdir()
    execution = SimpleNamespace(root=root, directory=lambda _: source.parent,
                                runtime=RepairRuntime())
    return RepairWorkspace(execution, "repair_" + "b" * 16,
                           {"id": "synthetic", "config": {"language": "python", "test": "suite"}},
                           lambda: None)


def tool(call, name, args):
    return ModelResponse(output=[ResponseFunctionToolCall(
        id=f"feedback_{call}", call_id=f"feedback_{call}", type="function_call",
        name=name, arguments=json.dumps(args),
    )], usage=Usage(requests=1), response_id=None)


class FeedbackModel(RepairModel):
    def __init__(self, workspace, *, failures=1, action="correct", on_feedback=lambda: None):
        super().__init__()
        self.workspace, self.failures, self.action = workspace, failures, action
        self.on_feedback = on_feedback
        self.submissions, self.feedback, self.duplicates = 0, [], 0

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.calls == 1:
            return tool(self.calls, "read_repair_source", {"path": "app.py", "candidate": False})
        failed = self.workspace.verification and not self.workspace.verification.get("passed")
        if failed:
            outputs = [ast.literal_eval(i["output"]) for i in input
                       if isinstance(i, dict) and i.get("type") == "function_call_output"]
            latest = outputs[-1]
            if latest.get("error") == "repair_tests_failed":
                self.feedback.append(latest)
                assert latest["verification"]["full_suite_after"]["exit_code"] == 1
                assert "existing contract" in latest["verification"]["full_suite_after"]["log"]
                assert "Do not weaken assertions" in latest["next"]
                assert kwargs["tools"] and not kwargs["handoffs"]
                self.on_feedback()
            if self.action == "handoff":
                return tool(self.calls, "transfer_to_independent_reviewer", {})
            if self.action == "unresolved":
                return ModelResponse(output=[ResponseOutputMessage(
                    id="unresolved", type="message", role="assistant", status="completed",
                    content=[ResponseOutputText(
                        type="output_text", annotations=[],
                        text="The behavior conflicts with the existing contract.",
                    )],
                )], usage=Usage(requests=1), response_id=None)
        if not self.workspace.verification or failed:
            patch = fixture_patch()
            if (failed and self.action in ("duplicate", "repeat")
                    and (self.action == "repeat" or not self.duplicates)):
                self.duplicates += 1
                patch["summary"] = "Only a different summary"
                patch["edits"].reverse()
                return tool(self.calls, "submit_and_test_repair", {"patch": patch})
            self.submissions += 1
            if self.submissions > 1:
                patch["edits"][0]["content"] = f"print('corrected {self.submissions}')\n"
                if self.action == "unsafe":
                    patch["edits"][0]["path"] = "../outside.py"
                if self.action == "wrong_hash":
                    patch["edits"][0]["original_sha256"] = "f" * 64
                if self.action == "old_test":
                    patch["edits"][0]["path"] = "tests/test_original.py"
            self.workspace.execution.runtime.full_after = int(self.submissions <= self.failures)
            self.workspace.execution.runtime.log = "Synthetic existing contract test result"
            return tool(self.calls, "submit_and_test_repair", {"patch": patch})
        if kwargs["handoffs"]:
            return tool(self.calls, kwargs["handoffs"][0].tool_name, {})
        if not any(getattr(t, "name", "") == "read_repair_verification" for t in kwargs["tools"]):
            assert self.workspace.verification["passed"]
        elif not getattr(self, "review_read", False):
            self.review_read = True
            return tool(self.calls, "read_repair_verification", {})
        return ModelResponse(output=[ResponseOutputMessage(
            id="review", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", annotations=[], text=json.dumps({
                "approved": True, "summary": "Synthetic independent review", "risks": [],
            }))],
        )], usage=Usage(requests=1), response_id=None)


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_failed_verification_is_fed_back_then_revised_or_bounded(workspace, failures):
    events, model = [], FeedbackModel(workspace, failures=failures)
    run = run_repair(model, {"finding": {}, "confirmation": {}},
                     lambda *i: events.append(i), workspace=workspace)
    if failures == 3:
        with pytest.raises(Exception) as error:
            asyncio.run(run)
        assert repair_failure_code(error.value) == "repair_tests_failed"
        assert not any(k == "handoff" for k, _ in events)
    else:
        result = asyncio.run(run)
        assert result["approved"] and result["handoffs"] == [
            "repair_agent -> independent_reviewer",
        ]
    revisions = min(failures + 1, 3)
    assert model.submissions == revisions and len(model.feedback) == min(failures, 2)
    assert [r["revision"] for r in workspace.verification_history] == list(range(1, revisions + 1))
    assert len(workspace.execution.runtime.test_calls) == 7 * revisions
    assert (workspace.original / "app.py").read_bytes() == b"print('ok')"
    assert (workspace.original / "tests/test_original.py").read_text() == "assert True\n"
    assert (workspace.root / "candidate/app.py").read_text() == "print('fixed')\n"
    if revisions > 1:
        candidate = workspace.root / "revision-2/candidate/app.py"
        assert candidate.read_text() == "print('corrected 2')\n"
    assert workspace.read_count == 1  # Evidence budgets are not reset per revision.


@pytest.mark.parametrize("action,code", [
    ("handoff", "repair_verification_required"), ("unresolved", "repair_tests_failed"),
    ("unsafe", "repair_unsafe_path"), ("wrong_hash", "repair_source_changed"),
    ("old_test", "repair_existing_tests_protected"),
])
def test_feedback_never_authorizes_early_handoff_or_unsafe_revision(workspace, action, code):
    events, model = [], FeedbackModel(workspace, action=action)
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                               lambda *i: events.append(i), workspace=workspace))
    assert repair_failure_code(error.value) == code
    assert len(workspace.verification_history) == 1 and workspace.revision == 1
    assert not any(k == "handoff" for k, _ in events)
    assert not (workspace.root / "revision-2").exists()
    assert any(k == "repair_unresolved" for k, _ in events) is (action == "unresolved")


def test_summary_only_or_reordered_patch_is_not_retested(workspace):
    model, events = FeedbackModel(workspace, action="duplicate"), []
    result = asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                                   lambda *i: events.append(i), workspace=workspace))
    assert result["approved"] and events.count(("patch_unchanged", "rejected")) == 1
    assert len(workspace.execution.runtime.test_calls) == 14


def test_persistent_identical_proposals_exhaust_model_budget_without_rerunning_tests(workspace):
    model = FeedbackModel(workspace, action="repeat")
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}}, lambda *_: None,
                               workspace=workspace))
    assert repair_failure_code(error.value) == "repair_tests_failed"
    assert model.calls <= 9 and len(workspace.verification_history) == 1
    assert len(workspace.execution.runtime.test_calls) == 7


@pytest.mark.parametrize("code", ["repair_paused", "repair_deadline_exceeded"])
def test_pause_or_deadline_after_tests_prevents_feedback_model_call(workspace, code):
    blocked = False
    original_run = workspace.execution.runtime.run

    def run(env, action, check):
        nonlocal blocked
        result = original_run(env, action, check)
        if env["id"].endswith("-2") and result[0] == 1:
            blocked = True
        return result

    def check():
        if blocked:
            raise Conflict(code)

    workspace.execution.runtime.run = run
    workspace.check = check
    model = FeedbackModel(workspace)
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}}, lambda *_: None,
                               workspace=workspace))
    assert repair_failure_code(error.value) == code and model.calls == 2


def test_feedback_logs_are_bounded_and_credentials_redacted():
    result = verification_feedback({"full_suite_after": {
        "exit_code": 1, "log": "x" * 5000 + '\napi_key="private-credential-value"',
        "command": "run", }, "untrusted": {"exit_code": 0, "log": "ignored"}})
    assert len(result["full_suite_after"]["log"]) <= 4000
    assert "private-credential-value" not in str(result) and "untrusted" not in result


def test_live_round_history_is_persisted_before_next_model_call(pending):  # noqa: F811
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    observed = []

    async def repair(context, event, *, workspace):
        model = FeedbackModel(workspace,
                              on_feedback=lambda: observed.append(repairs.get(task["id"])))
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert observed[0]["stage"] == "repair_agent"
    assert len(observed[0]["verification_rounds"]) == 1
    assert observed[0]["verification_rounds"][0]["verification"]["passed"] is False
    assert len(result["verification_rounds"]) == 2 and result["status"] == "pr_created"
    assert "_rounds" not in result["verification"] and len(publisher.calls) == 1


def test_failed_revision_preparation_preserves_prior_round_evidence(pending, monkeypatch):  # noqa: F811
    import repowatch.repair_workspace as module

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    original_copy = module.shutil.copytree

    def copytree(source, target, *args, **kwargs):
        if "revision-2" in target.parts:
            raise OSError("Synthetic disk failure")
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "copytree", copytree)

    async def repair(context, event, *, workspace):
        return await run_repair(FeedbackModel(workspace), context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "failed" and not publisher.calls
    assert len(result["verification_rounds"]) == 1
    assert result["verification_rounds"][0]["verification"]["full_suite_after"]["exit_code"] == 1
    assert result["verification"] == {}


def legacy_row():
    return {"status": "failed", "error": "repair_tests_failed", "attempt": 3,
            "context": "{}", "patch": "{}", "source_digest": "a" * 64,
            "review": None, "remote": None, "verification": json.dumps({"passed": False, **{
                k: {"exit_code": 1, "log": "Synthetic failure"} for k in (
                    "original_suite", "regression_before", "regression_after", "full_suite_after",
                )
            }})}


@pytest.mark.parametrize("case", ["eligible", "current_policy", "used", "review", "remote",
                                 "passed", "missing", "wrong_error", "not_exhausted"])
def test_old_verification_recovery_is_narrow(case):
    row = legacy_row()
    if case == "current_policy":
        row["context"] = json.dumps({"verification_policy": VERIFICATION_POLICY})
    elif case == "used":
        row["context"] = json.dumps({"verification_recovery_used": True})
    elif case in ("review", "remote"):
        row[case] = "{}"
    elif case == "passed":
        row["verification"] = '{"passed":true}'
    elif case == "missing":
        row["verification"] = '{"passed":false}'
    elif case == "wrong_error":
        row["error"] = "repair_unsafe_path"
    elif case == "not_exhausted":
        row["attempt"] = 2
    assert Repairs._verification_recovery(row) is (case == "eligible")


def test_exhausted_old_task_offers_one_explicit_recovery_with_prior_evidence(pending):  # noqa: F811
    repairs, _, _, _, _ = pending
    task = confirm(pending)
    enable(repairs)
    with repairs.store.connection() as db:
        values = legacy_row()
        context = json.loads(db.execute("SELECT context FROM repair_tasks WHERE id=?",
                                        (task["id"],)).fetchone()[0])
        db.execute("UPDATE repair_tasks SET status='failed',attempt=3,error='repair_tests_failed',"
                   "source_digest=?,patch=?,verification=?,context=? WHERE id=?",
                   (values["source_digest"], values["patch"], values["verification"],
                    json.dumps(context), task["id"]))
    failed = repairs.get(task["id"])
    assert failed["can_retry"] and failed["verification_recovery_available"]
    assert not asyncio.run(repairs.tick())  # Deployment/polling never queues it.
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["context"]["verification_recovery_used"]
    previous = queued["context"]["previous_failure"]["verification"]
    assert previous["full_suite_after"]["exit_code"] == 1
    assert queued["attempts"][-1]["attempt"] == 3
    assert queued["attempts"][-1]["error"] == "repair_tests_failed"
    assert repairs._attempt_limit({"context": json.dumps(queued["context"])}) == 4
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=4,error='repair_tests_failed' "
                   "WHERE id=?", (task["id"],))
    final = repairs.get(task["id"])
    assert not final["verification_recovery_available"] and not final["can_retry"]


def test_passed_patch_cannot_be_revised(workspace):
    workspace.read("app.py")
    workspace.prepare(Patch.model_validate(fixture_patch()))
    workspace.verify(lambda *_: None)
    changed = copy.deepcopy(fixture_patch())
    changed["edits"][0]["content"] = "print('later')\n"
    with pytest.raises(Conflict, match="repair_patch_already_submitted"):
        workspace.prepare(Patch.model_validate(changed), revise=True)


def test_returning_to_any_previous_failed_candidate_is_not_retested(workspace):
    workspace.read("app.py")
    workspace.execution.runtime.full_after = 1
    first = fixture_patch()
    for revision in range(2):
        patch = copy.deepcopy(first)
        patch["edits"][0]["content"] = f"print('candidate {revision}')\n"
        workspace.prepare(Patch.model_validate(patch), revise=bool(revision))
        with pytest.raises(Conflict, match="repair_tests_failed"):
            workspace.verify(lambda *_: None)
    first["edits"][0]["content"] = "print('candidate 0')\n"
    with pytest.raises(Conflict, match="repair_patch_unchanged"):
        workspace.prepare(Patch.model_validate(first), revise=True)
    assert workspace.revision == 2 and len(workspace.execution.runtime.test_calls) == 14


@pytest.mark.parametrize("code", ["repair_unsafe_path", "repair_source_changed", "repair_paused",
                                 "repair_deadline_exceeded", "repair_output_truncated"])
def test_prior_failed_tests_do_not_mask_new_specific_error(workspace, code):
    workspace.verification = {"passed": False, "regression_before": {"exit_code": 1}}
    assert Repairs._error_code(Conflict(code), workspace) == code


@pytest.mark.parametrize("failure,code", [("setup", "repair_setup_failed"),
                                        ("docker", "docker_unavailable")])
def test_infrastructure_failure_does_not_enter_patch_correction_loop(workspace, failure, code):
    if failure == "setup":
        workspace.execution.runtime.setup_code = 1
    else:
        workspace.execution.runtime.fail = True
    model, events = FeedbackModel(workspace), []
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                               lambda *i: events.append(i), workspace=workspace))
    assert repair_failure_code(error.value) == code and model.calls == 2
    assert not any(k == "verification_feedback" for k, _ in events)
