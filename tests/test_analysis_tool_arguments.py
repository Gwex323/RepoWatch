"""Regression coverage for validated, role-bound analysis tool calls."""

import asyncio
import json
import sqlite3
import time
from typing import Annotated

import pytest
from agents import AgentOutputSchema, Model, ModelResponse, ModelSettings, Usage, function_tool
from agents.function_schema import function_schema
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
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
    bounded_scope_issues,
    failure_event_payload,
    report_failure_fingerprint,
    report_progress_material,
    report_scope_feedback,
    report_scope_issues,
    run_analysis,
    scope_report_issue,
)
from repowatch.scope_models import ScopeRequirement
from repowatch.store import Conflict
from repowatch.task_scopes import (
    register_scope,
    request_scope_review,
    save_scope_review,
    scope_packet,
)


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


def test_scope_and_failure_events_keep_complete_safe_diagnostics(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    scope_detail = {
        "role": ROLES[-1],
        "issues": [{
            "exclusion_key": f"excluded:path_{index}.py",
            "target_key": f"registered_scope_item_{index}",
            "required_evidence": "client",
            "available_evidence": ["integration", "regression"],
            "reason": "missing_evidence_kind",
        } for index in range(12)],
    }
    failure_detail = {
        "code": "analysis_report_no_progress",
        "stage": ROLES[-1],
        "progress_fingerprint": "a" * 64,
        "budget": {"diagnostic": "bounded failure diagnostics remain durable"},
    }
    scope_encoded, failure_encoded = (
        json.dumps(scope_detail), json.dumps(failure_detail)
    )
    assert len(scope_encoded) > 1024 and len(failure_encoded) < 1024
    analyses.event(analysis_id, "scope_review_validation", scope_encoded)
    analyses.event(analysis_id, "failure", failure_encoded)
    row = analyses.get(analysis_id)
    saved = {e["kind"]: e["agent"] for e in row["events"]}
    assert json.loads(saved["scope_review_validation"]) == scope_detail
    assert json.loads(saved["failure"]) == failure_detail


def test_large_failure_budget_is_dropped_before_the_fixed_event_limit():
    fingerprints = "a" * 64
    details = {
        "code": "scope_review_incomplete",
        "stage": ROLES[-1],
        "progress_fingerprint": fingerprints,
        "budget": {"diagnostic": "x" * 4000},
    }
    encoded = failure_event_payload(details)
    saved = json.loads(encoded)
    assert len(encoded) <= 1000
    assert saved["progress_fingerprint"] == fingerprints
    assert "budget" not in saved


def test_failure_payload_falls_back_to_bounded_scalars_without_truncating_json():
    fingerprints = "b" * 64
    details = {
        "code": "scope_review_incomplete",
        "stage": ROLES[-1],
        "exception_type": "x" * 4000,
        "progress_fingerprint": fingerprints,
    }
    encoded = failure_event_payload(details)
    saved = json.loads(encoded)
    assert len(encoded) <= 1000
    assert saved["code"] == details["code"]
    assert saved["stage"] == details["stage"]
    assert saved["progress_fingerprint"] == fingerprints
    assert len(saved["exception_type"]) == 128


def test_failure_payload_has_a_hard_bound_for_every_optional_text_field():
    details = {
        key: "界" * 4000
        for key in (
            "code", "stage", "exception_type", "checkpoint_digest",
            "progress_fingerprint",
            "sqlite_errorcode", "http_status",
        )
    }
    details["elapsed_seconds"] = 10 ** 2000
    encoded = failure_event_payload(details)
    saved = json.loads(encoded)
    assert len(encoded) <= 1000
    assert len(saved["code"]) <= 128
    assert len(saved["stage"]) <= 128
    assert len(saved["checkpoint_digest"]) <= 64
    assert len(saved["progress_fingerprint"]) <= 64
    assert "elapsed_seconds" not in saved


def test_failure_payload_survives_event_storage_without_truncation(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    fingerprints = "d" * 64
    payload = failure_event_payload({
        "code": "scope_review_incomplete",
        "stage": ROLES[-1],
        "progress_fingerprint": fingerprints,
        "budget": {"diagnostic": "x" * 4000},
    })
    analyses.event(analysis_id, "failure", payload)
    saved = next(
        event["agent"] for event in analyses.get(analysis_id)["events"]
        if event["kind"] == "failure"
    )
    assert saved == payload
    assert len(saved) <= 1024
    assert json.loads(saved)["progress_fingerprint"] == fingerprints


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


def test_scope_report_issue_rejects_missing_scope_review():
    context = {"task_scope": {
        "version": 1,
        "items": [{"key": "behavior", "required_evidence": ["regression"]}],
        "execution_limits": [],
    }}
    review = Review(
        summary="Review",
        limitations=["Limited"],
        findings=[],
        scope_review=None,
    )
    assert scope_report_issue(review, context) == "scope_review_required"


def test_gui_exclusion_requires_client_evidence_before_final_review_acceptance():
    context = {"task_scope": {
        "version": 2,
        "items": [
            {"key": "trae_cache_adapter_safety",
             "required_evidence": ["integration", "regression"]},
            {"key": "windows_client_behavior", "required_evidence": ["client"]},
        ],
        "execution_limits": [{
            "key": "excluded:tests/test_display_names.py",
            "reasons": ["windows_native"],
        }],
    }}
    review = Review.model_validate({
        "summary": "Review",
        "limitations": ["Windows client was unavailable."],
        "findings": [],
        "scope_review": {
            "version": 2,
            "accepted": False,
            "rationale": "Coverage is incomplete.",
            "items": [
                {"key": "trae_cache_adapter_safety", "outcome": "blocked",
                 "rationale": "Client evidence is missing.", "evidence": [], "finding_ids": []},
                {"key": "windows_client_behavior", "outcome": "blocked",
                 "rationale": "Client evidence is missing.", "evidence": [], "finding_ids": []},
            ],
            "exclusions": [{
                "key": "excluded:tests/test_display_names.py",
                "outcome": "required",
                "required_keys": ["trae_cache_adapter_safety"],
                "rationale": "This file needs Windows-native evidence.",
                "evidence": [],
            }],
        },
    })
    assert scope_report_issue(review, context) == "scope_review_incomplete"
    issues = [{
        "exclusion_key": "excluded:tests/test_display_names.py",
        "target_key": "trae_cache_adapter_safety",
        "required_evidence": "client",
        "available_evidence": ["integration", "regression"],
        "reason": "missing_evidence_kind",
    }]
    assert report_scope_issues(review, context) == issues
    fingerprint = report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", review.model_dump(), context,
    )
    changed = review.model_copy(deep=True)
    changed.summary = "Different cosmetic wording."
    changed.scope_review.exclusions[0].rationale = "Different explanation."
    changed.scope_review.exclusions[0].required_keys = [
        "windows_client_behavior", "trae_cache_adapter_safety",
    ]
    assert report_scope_issues(changed, context) == issues
    assert report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", changed.model_dump(), context,
    ) == fingerprint
    review.scope_review.exclusions[0].required_keys = ["windows_client_behavior"]
    assert scope_report_issue(review, context) is None
    assert report_scope_issues(review, context) == []


def test_review_and_save_reject_the_same_gui_scope_semantics(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    with execution.store.connection() as db:
        row = dict(db.execute(
            "SELECT * FROM repository_analyses WHERE id=?", (analysis_id,),
        ).fetchone())
        context = json.loads(row["context"])
        context.setdefault("files", {})["app.py"] = "print('ok')\n"
        context["test_result"] = {"desktop_scope": {
            "excluded": [{
                "path": "tests/test_display_names.py",
                "reasons": ["windows_native"],
            }],
            "skipped": 0,
        }}
        db.execute(
            "UPDATE repository_analyses SET status='running',context=? WHERE id=?",
            (json.dumps(context), analysis_id),
        )
        row.update(status="running", context=json.dumps(context))
        first = register_scope(db, row, [{
            "key": "trae_cache_adapter_safety",
            "title": "TRAE adapter safety",
            "expected": "Adapter behavior remains safe.",
            "paths": ["app.py"],
            "required_evidence": ["integration", "regression"],
        }], ROLES[0])
        scope = register_scope(db, row, [{
            "key": "windows_client_behavior",
            "title": "Windows client behavior",
            "expected": "The native client behavior is verified.",
            "paths": ["app.py"],
            "required_evidence": ["client"],
        }], ROLES[0])
        assert scope["version"] == first["version"] + 1
        review = Review.model_validate({
            "summary": "Review",
            "limitations": ["Windows-native tests were excluded."],
            "findings": [],
            "scope_review": {
                "version": scope["version"],
                "accepted": False,
                "rationale": "Coverage is incomplete.",
                "items": [
                    {"key": "trae_cache_adapter_safety", "outcome": "blocked",
                     "rationale": "Client evidence is missing.", "evidence": [],
                     "finding_ids": []},
                    {"key": "windows_client_behavior", "outcome": "blocked",
                     "rationale": "Client evidence is missing.", "evidence": [],
                     "finding_ids": []},
                ],
                "exclusions": [{
                    "key": "excluded:tests/test_display_names.py",
                    "outcome": "required",
                    "required_keys": ["trae_cache_adapter_safety"],
                    "rationale": "The excluded file needs Windows-native evidence.",
                    "evidence": [],
                }],
            },
        })
        assert scope_report_issue(
            review, {"task_scope": scope},
        ) == "scope_review_incomplete"
        with pytest.raises(Conflict, match="scope_review_incomplete"):
            save_scope_review(db, row, review.scope_review.model_dump(), [])


@pytest.mark.parametrize(("outcome", "targets", "reason", "required_evidence"), [
    ("required", ["missing_requirement"], "unknown_scope_requirement", "client"),
    ("required", [], "missing_required_keys", "client"),
    ("unrelated", [], "unrelated_without_evidence", None),
])
def test_scope_semantic_feedback_covers_fixed_failure_reasons(
        outcome, targets, reason, required_evidence,
):
    context = {"task_scope": {
        "version": 1,
        "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
        "execution_limits": [{"key": "excluded:app.py", "reasons": ["gui"]}],
    }}
    review = Review.model_validate({
        "summary": "Review",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Coverage is incomplete.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": [],
            }],
            "exclusions": [{
                "key": "excluded:app.py",
                "outcome": outcome,
                "required_keys": targets,
                "rationale": "Fixture limitation.",
                "evidence": [],
            }],
        },
    })
    issues = report_scope_issues(review, context)
    assert len(issues) == 1
    assert issues[0]["reason"] == reason
    assert issues[0]["required_evidence"] == required_evidence


def test_scope_feedback_is_bounded_and_explicitly_reports_truncation():
    issues = [{
        "exclusion_key": f"excluded:{index:02d}:" + "x" * 120,
        "target_key": f"target_{index}",
        "required_evidence": "client",
        "available_evidence": ["integration", "regression"],
        "reason": "missing_evidence_kind",
    } for index in range(12)]
    feedback = bounded_scope_issues(issues)
    assert len(feedback["issues"]) == 6
    assert feedback["total"] == 12
    assert feedback["truncated"] is True
    encoded = json.dumps({"role": ROLES[-1], **feedback}, ensure_ascii=False)
    assert len(encoded) < 4096
    prompt = report_scope_feedback(issues)
    assert '"total":12' in prompt and '"truncated":true' in prompt


def test_scope_feedback_hashes_unregistered_candidate_keys():
    context = {"task_scope": {
        "items": [{"key": "registered_item", "required_evidence": ["client"]}],
        "execution_limits": [{"key": "registered_exclusion", "reasons": ["gui"]}],
    }}
    issues = [{
        "exclusion_key": "registered_exclusion",
        "target_key": "registered_item",
        "required_evidence": "client",
        "available_evidence": [],
        "reason": "missing_evidence_kind",
    }, {
        "exclusion_key": "candidate_private_exclusion",
        "target_key": "candidate_private_target",
        "required_evidence": "client",
        "available_evidence": [],
        "reason": "missing_evidence_kind",
    }]
    feedback = bounded_scope_issues(issues, context)
    assert feedback["issues"][0]["exclusion_key"] == "registered_exclusion"
    assert feedback["issues"][0]["target_key"] == "registered_item"
    assert feedback["issues"][1]["exclusion_key"].startswith("sha256:")
    assert feedback["issues"][1]["target_key"].startswith("sha256:")
    encoded = json.dumps(feedback)
    assert "candidate_private" not in encoded
    assert "candidate_private" not in report_scope_feedback(issues, context)


def test_scope_review_issues_deduplicates_required_keys():
    context = {"task_scope": {
        "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
        "execution_limits": [{"key": "excluded:app.py", "reasons": ["gui"]}],
    }}
    review = Review.model_validate({
        "summary": "Review",
        "limitations": ["Limited"],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Coverage is incomplete.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Client evidence is missing.",
                "evidence": [],
                "finding_ids": [],
            }],
            "exclusions": [{
                "key": "excluded:app.py",
                "outcome": "required",
                "required_keys": ["fixture_behavior", "fixture_behavior"],
                "rationale": "This needs client evidence.",
                "evidence": [],
            }],
        },
    })
    issues = report_scope_issues(review, context)
    assert len(issues) == 1
    assert issues[0]["reason"] == "missing_evidence_kind"


def test_failure_fingerprint_uses_only_safe_structural_material(monkeypatch):
    context = {"task_scope": {
        "version": 1,
        "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
        "execution_limits": [],
    }}
    material = []

    def capture(value):
        material.append(value)
        return "safe-fingerprint"

    monkeypatch.setattr("repowatch.repository_analysis.digest", capture)
    malformed = {
        "summary": "private-candidate-summary",
        "findings": [{"evidence": [{"quote": "private-source-quote"}]}],
        "scope_review": {"exclusions": "private-exclusion-text"},
    }
    changed = {
        "summary": "different-private-summary",
        "findings": [{"evidence": [{"quote": "different-private-quote"}]}],
        "scope_review": {"exclusions": "different-private-text"},
    }
    assert report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", malformed, context,
    ) == "safe-fingerprint"
    assert report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", changed, context,
    ) == "safe-fingerprint"
    assert material[0] == material[1]
    encoded = json.dumps(material, sort_keys=True)
    assert "private-" not in encoded and "candidate" not in encoded


def test_report_progress_material_uses_a_fixed_label_for_invalid_scope_review():
    context = {"task_scope": {
        "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
        "execution_limits": [],
    }}
    report = {"scope_review": {"exclusions": "private-invalid-structure"}}
    material = report_progress_material(
        "scope_review_incomplete", report, context,
    )
    assert material == {
        "code": "scope_review_incomplete",
        "scope_issues": [{
            "exclusion_key": None,
            "target_key": None,
            "required_evidence": None,
            "available_evidence": [],
            "reason": "invalid_scope_review_structure:TypeError",
        }],
    }


def test_failure_fingerprint_ignores_scope_review_order_without_semantic_issues(
        monkeypatch,
):
    context = {"task_scope": {
        "version": 1,
        "items": [
            {"key": "first", "required_evidence": ["regression"]},
            {"key": "second", "required_evidence": ["integration"]},
        ],
        "execution_limits": [],
    }}
    items = [{
        "key": "first",
        "outcome": "blocked",
        "rationale": "private rationale",
        "evidence": [],
        "finding_ids": [],
    }, {
        "key": "second",
        "outcome": "investigating",
        "rationale": "different private rationale",
        "evidence": [],
        "finding_ids": [],
    }]
    first = {
        "summary": "private summary one",
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "private review rationale one",
            "items": items,
            "exclusions": [],
        },
    }
    second = {
        "summary": "private summary two",
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "private review rationale two",
            "items": list(reversed(items)),
            "exclusions": [],
        },
    }
    assert report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", first, context,
    ) == report_failure_fingerprint(
        "scope_review_incomplete", "same-checkpoint", second, context,
    )


def test_repeated_invalid_scope_semantics_stops_without_replaying_forever():
    class InvalidScopeReview(ReviewModel):
        attempt = 0

        async def get_response(self, *args, **kwargs):
            result = await super().get_response(*args, **kwargs)
            if result.output and result.output[0].type == "message":
                content = result.output[0].content[0]
                data = json.loads(content.text)
                if data.get("scope_review"):
                    self.attempt += 1
                    data["scope_review"]["exclusions"] = [{
                        "key": "excluded:app.py",
                        "outcome": "required",
                        "required_keys": ["fixture_behavior"],
                        "rationale": (
                            f"The excluded file needs client evidence ({self.attempt})."
                        ),
                        "evidence": [],
                    }]
                    content.text = json.dumps(data)
            return result

    events = []

    def event(kind, value):
        events.append((kind, value))
        if kind == "scope_batch_check":
            return []
        if kind == "scope_batch_execute":
            packet = json.loads(value)
            item = packet["batches"][0][0]
            return [{"version": 2, "items": [item], "execution_limits": [{
                "key": "excluded:app.py", "reasons": ["windows_native"],
            }]}]

    context = {
        "files": {"app.py": "print('ok')"},
        "config": {},
        "jobs": [],
        "test_result": None,
        "task_scope": {"version": 1, "items": []},
    }
    with pytest.raises(AnalysisFailure, match="analysis_report_no_progress"):
        asyncio.run(run_analysis(InvalidScopeReview(), context, event))
    validations = [json.loads(value) for kind, value in events
                   if kind == "scope_review_validation"]
    assert validations == [{
        "role": ROLES[-1],
        "issues": [{
            "exclusion_key": "excluded:app.py",
            "target_key": "fixture_behavior",
            "required_evidence": "client",
            "available_evidence": ["regression"],
            "reason": "missing_evidence_kind",
        }],
        "total": 1,
        "truncated": False,
    }]
    assert sum(kind == "format_retry" for kind, _ in events) == 1


def test_changed_review_checkpoint_allows_same_invalid_scope_semantics():
    context = {
        "task_scope": {
            "version": 1,
            "items": [{
                "key": "fixture_behavior",
                "required_evidence": ["regression"],
            }],
            "execution_limits": [{
                "key": "excluded:app.py",
                "reasons": ["windows_native"],
            }],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Review candidate.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Fixture only.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": [],
            }],
            "exclusions": [{
                "key": "excluded:app.py",
                "outcome": "required",
                "required_keys": ["fixture_behavior"],
                "rationale": "Client evidence is required.",
                "evidence": [],
            }],
        },
    }

    class RepeatingInvalidReview(Model):
        calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            self.calls += 1
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"repeat_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(review),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    model = RepeatingInvalidReview()
    digests = ("checkpoint-one", "checkpoint-two", "checkpoint-two")
    events = []
    stage = StageModel(
        model,
        ROLES[-1],
        StageBudget(),
        lambda kind, value: events.append((kind, value)),
        review_contract=lambda _review: "scope_review_incomplete",
        scope_context=lambda: context,
        checkpoint_digest=lambda: digests[model.calls - 1],
        update_private_correction_checkpoint=lambda _value: None,
    )
    with pytest.raises(AnalysisFailure, match="analysis_report_no_progress"):
        asyncio.run(stage.get_response(
            "Trusted final-review instructions",
            json.dumps({"files": {"app.py": "print('ok')"}}),
            tools=[],
            handoffs=[],
            output_schema=AgentOutputSchema(Review),
            model_settings=ModelSettings(max_tokens=6000),
        ))
    assert model.calls == 3
    assert sum(kind == "scope_review_validation" for kind, _ in events) == 2
    assert sum(kind == "format_retry" for kind, _ in events) == 2


def test_mapping_retry_resets_progress_when_checkpoint_changes(monkeypatch):
    context = {
        "task_scope": {
            "version": 1,
            "items": [{
                "key": "fixture_behavior",
                "required_evidence": ["regression"],
            }],
            "execution_limits": [],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Review candidate.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Fixture only.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": ["missing_finding"],
            }],
            "exclusions": [],
        },
    }
    mapping_issue = {
        "section": "scope_items",
        "item": 1,
        "field": "finding_ids",
        "reason": "unknown_finding_id",
        "positions": [1],
    }
    monkeypatch.setattr(
        "repowatch.repository_analysis.report_mapping_issues",
        lambda *_args: [mapping_issue],
    )

    class RepeatingMapping(Model):
        calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            self.calls += 1
            schema = kwargs.get("output_schema")
            properties = schema.json_schema().get("properties", {}) if schema else {}
            payload = review
            if "edits" in properties:
                payload = {"edits": [{
                    "section": "scope_items",
                    "item": 1,
                    "references": [],
                    "new_findings": [],
                    "unresolved": True,
                    "rationale": "No persisted finding can be linked.",
                }]}
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"mapping_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(payload),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    model = RepeatingMapping()
    digests = ("checkpoint-one", "checkpoint-two", "checkpoint-two")
    events = []
    stage = StageModel(
        model,
        ROLES[-1],
        StageBudget(),
        lambda kind, value: events.append((kind, value)),
        review_contract=lambda _review: "scope_mapping_invalid",
        scope_context=lambda: context,
        checkpoint_digest=lambda: digests[model.calls - 1],
        update_private_correction_checkpoint=lambda _value: None,
    )
    with pytest.raises(AnalysisFailure, match="analysis_report_no_progress"):
        asyncio.run(stage.get_response(
            "Trusted final-review instructions",
            json.dumps({"files": {"app.py": "print('ok')"}}),
            tools=[],
            handoffs=[],
            output_schema=AgentOutputSchema(Review),
            model_settings=ModelSettings(max_tokens=6000),
        ))
    assert model.calls == 3
    assert sum(kind == "scope_mapping_validation" for kind, _ in events) == 2


def test_scope_semantics_take_precedence_even_if_contract_reports_mapping_invalid():
    context = {
        "task_scope": {
            "version": 1,
            "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
            "execution_limits": [{"key": "excluded:app.py", "reasons": ["windows_native"]}],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Review candidate.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Fixture only.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": ["missing_finding"],
            }],
            "exclusions": [{
                "key": "excluded:app.py",
                "outcome": "required",
                "required_keys": ["fixture_behavior"],
                "rationale": "Client evidence is required.",
                "evidence": [],
            }],
        },
    }

    class MappingContractModel(Model):
        calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            self.calls += 1
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"mapping_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(review),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    events = []
    stage = StageModel(
        MappingContractModel(),
        ROLES[-1],
        StageBudget(),
        lambda kind, value: events.append((kind, value)),
        review_contract=lambda _review: "scope_mapping_invalid",
        scope_context=lambda: context,
        update_private_correction_checkpoint=lambda _value: None,
    )
    with pytest.raises(AnalysisFailure, match="analysis_report_no_progress"):
        asyncio.run(stage.get_response(
            "Trusted final-review instructions",
            json.dumps({"files": {"app.py": "print('ok')"}}),
            tools=[],
            handoffs=[],
            output_schema=AgentOutputSchema(Review),
            model_settings=ModelSettings(max_tokens=6000),
        ))
    assert sum(kind == "scope_review_validation" for kind, _ in events) == 1
    assert not any(kind == "scope_mapping_validation" for kind, _ in events)


def test_first_generic_review_correction_clears_any_stale_private_checkpoint():
    context = {
        "task_scope": {
            "version": 1,
            "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
            "execution_limits": [],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Review candidate.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Fixture only.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": [],
            }],
            "exclusions": [],
        },
    }

    class GenericIssueModel(Model):
        calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            self.calls += 1
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"generic_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(review),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    contracts = iter(["scope_review_incomplete", None])
    saved = []
    model = GenericIssueModel()
    stage = StageModel(
        model,
        ROLES[-1],
        StageBudget(),
        lambda *_: None,
        review_contract=lambda _review: next(contracts),
        scope_context=lambda: context,
        update_private_correction_checkpoint=saved.append,
    )
    result = asyncio.run(stage.get_response(
        "Trusted final-review instructions",
        json.dumps({"files": {"app.py": "print('ok')"}}),
        tools=[],
        handoffs=[],
        output_schema=AgentOutputSchema(Review),
        model_settings=ModelSettings(max_tokens=6000),
    ))
    assert result.output
    assert saved == [None]


def test_scope_contract_mismatch_emits_value_free_diagnostic():
    context = {
        "task_scope": {
            "version": 2,
            "items": [{"key": "fixture_behavior", "required_evidence": ["regression"]}],
            "execution_limits": [],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Private candidate summary.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "Private candidate rationale.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": [],
            }],
            "exclusions": [],
        },
    }

    class VersionMismatchModel(Model):
        async def get_response(self, system_instructions, input, **kwargs):
            return ModelResponse(output=[ResponseOutputMessage(
                id="version_mismatch",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(review),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    events = []
    stage = StageModel(
        VersionMismatchModel(),
        ROLES[-1],
        StageBudget(),
        lambda kind, value: events.append((kind, value)),
        review_contract=lambda _review: "scope_review_incomplete",
        scope_context=lambda: context,
        update_private_correction_checkpoint=lambda _value: None,
    )
    with pytest.raises(AnalysisFailure, match="analysis_report_no_progress"):
        asyncio.run(stage.get_response(
            "Trusted final-review instructions",
            json.dumps({"files": {"app.py": "print('ok')"}}),
            tools=[],
            handoffs=[],
            output_schema=AgentOutputSchema(Review),
            model_settings=ModelSettings(max_tokens=6000),
        ))
    diagnostics = [
        json.loads(value) for kind, value in events
        if kind == "scope_review_validation"
    ]
    assert len(diagnostics) == 1
    detail = diagnostics[0]["issues"][0]
    assert detail == {
        "reason": "scope_review_contract_mismatch",
        "registered_version": 2,
        "provided_version": 1,
        "item_counts": {"registered": 1, "provided": 1},
        "exclusion_counts": {"registered": 0, "provided": 0},
    }
    assert "Private candidate" not in json.dumps(diagnostics)


def test_generic_review_correction_clears_stale_private_mapping_checkpoint():
    context = {
        "task_scope": {
            "version": 1,
            "items": [{
                "key": "fixture_behavior",
                "required_evidence": ["regression"],
            }],
            "execution_limits": [],
        },
        "current_scope_findings": [],
    }
    review = {
        "summary": "Review candidate.",
        "limitations": ["Fixture only."],
        "findings": [],
        "scope_review": {
            "version": 1,
            "accepted": False,
            "rationale": "The mapping still needs review.",
            "items": [{
                "key": "fixture_behavior",
                "outcome": "blocked",
                "rationale": "Fixture only.",
                "evidence": [],
                "finding_ids": ["missing_finding"],
            }],
            "exclusions": [],
        },
    }

    class ScopedModel(Model):
        calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            self.calls += 1
            if self.calls == 2:
                payload = {"edits": [{
                    "section": "scope_items",
                    "item": 1,
                    "references": [],
                    "new_findings": [],
                    "unresolved": True,
                    "rationale": "No persisted finding can be linked.",
                }]}
            else:
                payload = review
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"review_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(
                    type="output_text",
                    text=json.dumps(payload),
                    annotations=[],
                )],
            )], usage=Usage(requests=1), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError()
            yield

    contracts = iter(["scope_mapping_invalid", "scope_review_incomplete", None])
    saved = []
    model = ScopedModel()
    stage = StageModel(
        model,
        ROLES[-1],
        StageBudget(),
        lambda *_: None,
        review_contract=lambda _review: next(contracts),
        scope_context=lambda: context,
        update_private_correction_checkpoint=saved.append,
    )
    result = asyncio.run(stage.get_response(
        "Trusted final-review instructions",
        json.dumps({"files": {"app.py": "print('ok')"}}),
        tools=[],
        handoffs=[],
        output_schema=AgentOutputSchema(Review),
        model_settings=ModelSettings(max_tokens=6000),
    ))
    assert result.output
    assert len(saved) == 3
    assert saved[0]["kind"] == "scope_mapping_correction"
    assert saved[1]["kind"] == "scope_mapping_correction"
    assert saved[2] is None


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
