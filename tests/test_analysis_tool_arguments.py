"""Regression coverage for validated, role-bound analysis tool calls."""

import asyncio
import json
import sqlite3
import time
from typing import Annotated

import pytest
from agents import Model, ModelResponse, ModelSettings, Usage, function_tool
from agents.function_schema import function_schema
from openai.types.responses import ResponseFunctionToolCall
from openai.types.shared import Reasoning
from pydantic import Field
from test_execution import service  # noqa: F401
from test_repository_analysis import Engine, ReviewModel, prepare_tested

from repowatch.analysis_tools import (
    SCOPE_BATCH_SIZE,
    tool_allowed,
    tool_argument_issue,
    tools_for_role,
)
from repowatch.repository_analysis import (
    ROLES,
    Analyses,
    AnalysisFailure,
    RetryBudget,
    Review,
    StageBudget,
    StageModel,
    run_analysis,
    scope_report_issue,
)
from repowatch.scope_models import ScopeRequirement
from repowatch.store import Conflict
from repowatch.task_scopes import register_scope, request_scope_review, scope_packet


def register_task_scope(
    items: Annotated[list[ScopeRequirement], Field(min_length=1, max_length=SCOPE_BATCH_SIZE)],
) -> dict:
    """Append bounded scope requirements."""
    return {}


def read_source_file(path: str, start_line: int, end_line: int) -> dict:
    """Read source."""
    return {}


FUNCTIONS = [register_task_scope, read_source_file]
TOOLS = [function_tool(fn) for fn in FUNCTIONS]
MODELS = {fn.__name__: function_schema(fn).params_pydantic_model for fn in FUNCTIONS}
ITEM = {"key": "behavior", "title": "Behavior", "expected": "Original acceptance behavior",
        "paths": ["app.py"], "required_evidence": ["regression"]}


def response(*calls):
    return ModelResponse(output=[ResponseFunctionToolCall(
        type="function_call", id=f"call_{i}", call_id=f"call_{i}", name=name,
        arguments=args if isinstance(args, str) else json.dumps(args),
    ) for i, (name, args) in enumerate(calls)], usage=Usage(requests=1), response_id=None)


def issue(name, args):
    return tool_argument_issue(response((name, args)), TOOLS, [], MODELS)


def test_nested_enum_diagnostic_uses_contract_and_never_model_value():
    bad = {**ITEM, "required_evidence": ["SECRET_PROVIDER_VALUE"]}
    detail, _ = issue("register_task_scope", {"items": [bad]})
    assert detail["issues"] == [{"field": "items[0].required_evidence[0]",
                                "code": "enum_value", "expected": {
                                    "enum": ["regression", "integration", "client"],
                                    "type": "string"}}]
    assert "SECRET" not in json.dumps(detail)


def test_unknown_nested_key_is_sanitized_and_json_location_is_numeric():
    detail, _ = issue("register_task_scope", {"items": [{**ITEM, "SECRET_KEY": "SECRET_VALUE"}]})
    assert detail["issues"][0]["field"] == "items[0].unexpected_field"
    assert "SECRET" not in json.dumps(detail)
    detail, _ = issue("register_task_scope", '{"items":[\nSECRET')
    assert detail["reason"] == "invalid_json"
    assert detail["line"] == 2 and detail["column"] == 1
    assert detail["chars"] == 17
    assert "SECRET" not in json.dumps(detail)


@pytest.mark.parametrize("size,valid", [(0, False), (1, True), (3, True), (4, False)])
def test_scope_batch_bounds(size, valid):
    problem = issue("register_task_scope", {"items": [ITEM] * size})
    assert (problem is None) is valid
    if not valid:
        assert problem[0]["issues"][0]["field"] == "items"


def test_role_permissions_match_advertised_tools():
    for role in ROLES:
        visible = {tool.name for tool in tools_for_role(role, TOOLS)}
        assert "read_source_file" in visible
        assert ("register_task_scope" in visible) == (role != "reproduction_assistant")
        assert ("register_task_scope" in visible) == tool_allowed(role, "register_task_scope")


class Script(Model):
    def __init__(self, replies, observation=None):
        self.replies = iter(replies)
        self.observation = observation or {}
        self.requests = []

    async def get_response(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.replies)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


def stage(delegate, role=ROLES[0], events=None, retries=None, **kwargs):
    return StageModel(delegate, role, StageBudget(),
                      lambda *event: events.append(event) if events is not None else None,
                      retries=retries, tool_parameter_models=MODELS, **kwargs)


async def call(model, *, tools=TOOLS, high=False):
    return await model.get_response("Trusted instructions", "Saved evidence", tools=tools,
                                    handoffs=[], output_schema=None,
                                    model_settings=ModelSettings(max_tokens=3000,
                                        reasoning=Reasoning(effort="high") if high else None))


def test_cap_inference_expands_budget_but_keeps_high_effort_and_observations():
    good = response(("register_task_scope", {"items": [ITEM]}))
    delegate = Script([response(("register_task_scope", '{"items":[')), good],
                      {"output_tokens": 8000, "reasoning_tokens": 7000,
                       "finish_reason": "tool_calls", "truncated": False})
    events = []
    result = asyncio.run(call(stage(delegate, events=events), high=True))
    assert result is good
    assert [r["model_settings"].max_tokens for r in delegate.requests] == [8000, 16000]
    assert all(r["model_settings"].reasoning.effort == "high" for r in delegate.requests)
    assert delegate.requests[0]["input"] == "Saved evidence"
    assert delegate.requests[1]["input"][0] == {"role": "user", "content": "Saved evidence"}
    diagnostic = json.loads(next(v for k, v in events if k == "tool_arguments_invalid"))
    assert diagnostic["suspected_output_limit"] is True
    assert any(kind == "tool_output_retry" for kind, _ in events)


def test_tool_correction_reserves_each_request_and_persists_shared_retry_budget():
    from repowatch import report_scheduler

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE repository_analyses(id TEXT PRIMARY KEY)")
    report_scheduler.initialize(db)
    db.execute("INSERT INTO repository_analyses(id) VALUES('analysis_fixture')")
    report_scheduler.begin(db, "analysis_fixture", time.time())

    def event(kind, value):
        if kind == "analysis_retry_budget":
            report_scheduler.save_budget(db, "analysis_fixture", json.loads(value))
        elif kind == "analysis_request":
            db.execute("UPDATE analysis_report_recovery SET requests=requests+1 "
                       "WHERE analysis_id='analysis_fixture'")
            return {"remaining_seconds": 30}

    delegate = Script([response(("read_source_file", '{"path":')),
                       response(("read_source_file", {
                           "path": "app.py", "start_line": 1, "end_line": 1}))])
    model = StageModel(delegate, ROLES[0], StageBudget(), event,
                       tool_parameter_models=MODELS)
    try:
        asyncio.run(call(model))
        saved = report_scheduler.row(db, "analysis_fixture")
        assert saved["requests"] == len(delegate.requests) == 2
        counters = json.loads(saved["retry_budget"])
        assert counters["tool_arguments_used"] == 1
        report_scheduler.save_budget(db, "analysis_fixture", {})
        assert json.loads(report_scheduler.row(db, "analysis_fixture")["retry_budget"]) == counters
    finally:
        db.close()


def test_valid_json_at_cap_and_schema_errors_do_not_trigger_expansion():
    events = []
    delegate = Script([response(("register_task_scope", {"items": [{**ITEM,
                        "required_evidence": ["wrong"]}]})),
                       response(("register_task_scope", {"items": [ITEM]}))],
                      {"output_tokens": 8000, "finish_reason": "tool_calls"})
    asyncio.run(call(stage(delegate, events=events), high=True))
    assert [r["model_settings"].max_tokens for r in delegate.requests] == [8000, 8000]
    assert not any(kind == "tool_output_retry" for kind, _ in events)
    assert "items[0].required_evidence[0]" in delegate.requests[1]["system_instructions"]


def test_correction_budget_is_shared_across_roles_and_stops_without_execution():
    retries = RetryBudget()
    bad = response(("read_source_file", '{"path":'))
    first = Script([bad, bad, response(("read_source_file", {
        "path": "app.py", "start_line": 1, "end_line": 1}))])
    asyncio.run(call(stage(first, retries=retries)))
    assert retries.tool_arguments_used == 2
    second = Script([bad, response(("read_source_file", {
        "path": "app.py", "start_line": 1, "end_line": 1}))])
    asyncio.run(call(stage(second, role=ROLES[1], retries=retries)))
    assert len(second.requests) == 2 and retries.tool_arguments_used == 3


def test_expansion_exhaustion_is_terminal_in_diagnostics(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.MAX_OUTPUT_TOKENS", 8000)
    events = []
    delegate = Script([response(("read_source_file", '{"path":'))], {"output_tokens": 8000})
    with pytest.raises(AnalysisFailure, match="analysis_output_truncated"):
        asyncio.run(call(stage(delegate, events=events, retries=RetryBudget(output_used=2)),
                         high=True))
    detail = json.loads(next(value for kind, value in events if kind == "tool_arguments_invalid"))
    assert detail["recoverable"] is False
    assert not any(kind == "tool_arguments_retry" for kind, _ in events)


def test_forbidden_tool_is_rejected_before_any_sdk_handler():
    delegate = Script([response(("register_task_scope", {"items": [ITEM]}))])
    with pytest.raises(AnalysisFailure, match="analysis_tool_not_allowed"):
        asyncio.run(call(stage(delegate, role=ROLES[2]), tools=[TOOLS[1]]))


def test_deadline_and_cancel_stop_argument_correction():
    async def scenario(cancel):
        class Stopping(Script):
            async def get_response(self, **kwargs):
                if cancel:
                    raise asyncio.CancelledError
                model.deadline = time.monotonic() - 1
                return response(("read_source_file", '{"path":'))
        model = stage(Stopping([]))
        await call(model)
    with pytest.raises(AnalysisFailure, match="analysis_deadline_exceeded"):
        asyncio.run(scenario(False))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario(True))


def test_native_runner_rejects_whole_batch_and_keeps_prior_real_reads():
    events = []
    class Mixed(ReviewModel):
        invalid_sent = False
        corrected_sent = False
        async def get_response(self, *args, **kwargs):
            if self.calls == 2 and not self.invalid_sent:
                self.invalid_sent = True
                return response(("read_source_file", {"path": "app.py", "start_line": 2,
                                                     "end_line": 2}),
                                ("register_task_scope", {"items": [{**ITEM,
                                    "required_evidence": ["wrong"]}]}))
            if self.invalid_sent and not self.corrected_sent:
                self.corrected_sent = True
                return response(("read_source_file", {"path": "app.py", "start_line": 2,
                                                     "end_line": 2}),
                                ("register_task_scope", {"items": [ITEM]}))
            return await super().get_response(*args, **kwargs)
    def event(kind, value):
        events.append((kind, value))
        if kind == "scope_proposal":
            return {"version": 1, "items": json.loads(value), "execution_limits": []}
    report = asyncio.run(run_analysis(Mixed(), {"files": {"app.py": "print('ok')\npass"},
        "config": {}, "jobs": [], "test_result": None}, event))
    assert report["execution_read"] and report["files_read"] == ["app.py"]
    assert sum(kind == "source" for kind, _ in events) == 2
    assert sum(kind == "evidence" for kind, _ in events) == 1
    assert sum(kind == "scope_proposal" for kind, _ in events) == 1
    assert sum(kind == "handoff" for kind, _ in events) == 3


def test_scope_is_registered_successfully_before_first_handoff():
    events, registered = [], []
    class Scoped(ReviewModel):
        async def get_response(self, *args, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                return response(("register_task_scope", {"items": [ITEM]}))
            return await super().get_response(*args, **kwargs)
    def event(kind, value):
        events.append((kind, value))
        if kind == "scope_batch_check":
            return []
        if kind == "scope_batch_execute":
            registered.extend(json.loads(value)["batches"][0])
            return [{"version": 2, "items": list(registered), "execution_limits": []}]
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [],
               "test_result": None, "task_scope": {"version": 1, "items": []}}
    report = asyncio.run(run_analysis(Scoped(), context, event))
    assert report["handoffs"]
    assert context["task_scope"]["version"] == 2
    kinds = [k for k, _ in events]
    assert kinds.index("scope_registered") < kinds.index("handoff")


def test_scope_service_failure_cannot_be_treated_as_registration():
    class Scoped(ReviewModel):
        async def get_response(self, *args, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                return response(("register_task_scope", {"items": [ITEM]}))
            return await super().get_response(*args, **kwargs)
    with pytest.raises(AnalysisFailure, match="analysis_scope_unregistered"):
        asyncio.run(run_analysis(Scoped(), {"files": {"app.py": "print('ok')"}, "config": {},
            "jobs": [], "test_result": None, "task_scope": {"version": 1, "items": []}}))


def test_durable_scope_policy_batches_and_immutability(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    with execution.store.connection() as db:
        row = dict(db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (analysis_id,)).fetchone())
        row.update(status="running", context=json.dumps({"files": {"app.py": "print('ok')"}}))
        db.execute("UPDATE repository_analyses SET status=?,context=? WHERE id=?",
                   (row["status"], row["context"], analysis_id))
        before = scope_packet(db, analysis_id)
        for role in ("reproduction_assistant", "unregistered_role"):
            with pytest.raises(Conflict, match="scope_role_required"):
                register_scope(db, row, [ITEM], role)
        assert scope_packet(db, analysis_id) == before
        first = register_scope(db, row, [ITEM], ROLES[0])
        assert first["version"] == before["version"] + 1
        assert register_scope(db, row, [ITEM], ROLES[1]) == first
        second = register_scope(db, row, [{**ITEM, "key": "sibling"}], ROLES[1])
        assert second["version"] == first["version"] + 1
        assert len(second["items"]) == 2
        with pytest.raises(Conflict, match="scope_requirement_immutable"):
            register_scope(db, row, [{**ITEM, "expected": "Weakened behavior"}], ROLES[3])
        assert scope_packet(db, analysis_id) == second


@pytest.mark.parametrize("actor", [None, "system"])
def test_scope_reassessment_preserves_requirements_and_records_caller(service, actor):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    with execution.store.connection() as db:
        row = dict(db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (analysis_id,)).fetchone())
        row.update(status="running", context=json.dumps({"files": {"app.py": "print('ok')"}}))
        db.execute("UPDATE repository_analyses SET status=?,context=? WHERE id=?",
                   (row["status"], row["context"], analysis_id))
        before = register_scope(db, row, [ITEM], ROLES[0])
        context = (request_scope_review(db, row) if actor is None
                   else request_scope_review(db, row, actor=actor))
        after = scope_packet(db, analysis_id)
        assert after["version"] == before["version"] + 1
        assert after["items"] == before["items"]
        assert context["current_scope_findings"] == []
        event = db.execute("SELECT actor,version FROM task_scope_events "
                           "WHERE scope_id=? AND kind='reassessment_requested' ORDER BY id DESC",
                           (after["id"],)).fetchone()
        assert event["actor"] == (actor or "owner")
        assert event["version"] == after["version"]


def test_diagnostic_event_storage_keeps_complete_safe_json(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    detail, _ = issue("register_task_scope", {"items": [{**ITEM,
        "required_evidence": ["wrong"]}] * 3})
    encoded = json.dumps(detail)
    assert len(encoded) > 240
    analyses.event(analysis_id, "tool_arguments_invalid", encoded)
    row = analyses.get(analysis_id)
    saved = next(e["agent"] for e in row["events"] if e["kind"] == "tool_arguments_invalid")
    assert json.loads(saved) == detail


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "scope_review_required"), ("version", "scope_review_incomplete"),
    ("key", "scope_review_incomplete"), ("exclusion", "scope_review_incomplete"),
    ("valid", None),
])
def test_report_requires_current_scope_keys_and_execution_classifications(mutation, expected):
    context = {"task_scope": {"version": 2, "items": [ITEM],
                               "execution_limits": [{"key": "windows"}]}}
    review = {"version": 2, "accepted": False, "rationale": "Not verified",
              "items": [{"key": "behavior", "outcome": "blocked", "rationale": "Limited",
                         "evidence": []}],
              "exclusions": [{"key": "windows", "outcome": "unknown", "required_keys": [],
                              "rationale": "Unavailable", "evidence": []}]}
    if mutation == "missing":
        review = None
    elif mutation == "version":
        review["version"] = 1
    elif mutation == "key":
        review["items"][0]["key"] = "other"
    elif mutation == "exclusion":
        review["exclusions"] = []
    report = Review(summary="Review", limitations=["Unavailable"], findings=[], scope_review=review)
    assert scope_report_issue(report, context) == expected


@pytest.mark.parametrize("persistent", [False, True])
def test_missing_scope_report_is_corrected_from_same_real_evidence(persistent):
    events, packets = [], []
    class MissingScope(ReviewModel):
        omitted = False
        async def get_response(self, *args, **kwargs):
            result = await super().get_response(*args, **kwargs)
            if result.output and result.output[0].type == "message":
                packets.append(kwargs["input"])
                if persistent or not self.omitted:
                    data = json.loads(result.output[0].content[0].text)
                    data["scope_review"] = None
                    result.output[0].content[0].text = json.dumps(data)
                    self.omitted = True
            return result
    def event(kind, value):
        events.append((kind, value))
        if persistent and kind == "format_retry" and len(packets) == 3:
            raise AnalysisFailure("analysis_report_invalid")  # Caller stops fixture.
        if kind == "scope_batch_check":
            return []
        if kind == "scope_batch_execute":
            return [{"version": 2, "items": json.loads(value)["batches"][0],
                     "execution_limits": []}]
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [],
               "test_result": None, "task_scope": {"version": 1, "items": []}}
    if persistent:
        with pytest.raises(AnalysisFailure, match="analysis_report_invalid"):
            asyncio.run(run_analysis(MissingScope(), context, event))
    else:
        report = asyncio.run(run_analysis(MissingScope(), context, event))
        assert report["scope_review"]["version"] == 2
        assert len(report["scope_review"]["items"]) == 1
    assert len(packets) == (3 if persistent else 2)
    assert events.count(("evidence", "execution_records")) == 1
    assert events.count(("source", "app.py")) == 1
    assert events.count(("format_retry", ROLES[3])) == (3 if persistent else 1)
    assert sum(k == "handoff" for k, _ in events) == 3
