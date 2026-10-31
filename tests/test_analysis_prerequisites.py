"""Required evidence must be collected through SDK tools before handoffs."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from agents import ModelResponse, ModelSettings, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
)
from test_execution import service  # noqa: F401
from test_repository_analysis import Engine, ReviewModel, prepare_tested

from repowatch.repository_analysis import (
    ROLES,
    Analyses,
    AnalysisFailure,
    StageBudget,
    StageModel,
    failure_code,
    run_analysis,
    validate_report,
)


def context():
    return {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [],
            "test_result": {"exit_code": 0}, "access_result": {"status": 200}}


def scope_context():
    value = context()
    value["task_scope"] = {"version": 1, "items": []}
    return value


def record_scope(events):
    def record(kind, value):
        events.append((kind, value))
        if kind in ("scope_batch_check", "scope_batch_execute"):
            packet = json.loads(value)
            return [{"version": packet["version"], "items": packet["batches"][0]}]
        return None
    return record


class GateAwareModel(ReviewModel):
    def __init__(self):
        super().__init__(findings=False)
        self.requests = []

    async def get_response(self, system_instructions, input, **kwargs):
        self.requests.append(kwargs)
        choice = kwargs["model_settings"].tool_choice
        if choice in ("read_execution_records", "read_source_file"):
            self.calls += 1
            args = {} if choice == "read_execution_records" else {
                "path": "app.py", "start_line": 1, "end_line": 1,
            }
            return ModelResponse(output=[ResponseFunctionToolCall(
                id=f"required_{self.calls}", call_id=f"required_{self.calls}",
                type="function_call", name=choice, arguments=json.dumps(args),
            )], usage=Usage(requests=1), response_id=None)
        # Without the gate this model reads source, then immediately hands off,
        # reproducing the live failure: all three handoffs, no execution tool read.
        if self.calls == 1:
            self.calls += 1
            return ModelResponse(output=[ResponseFunctionToolCall(
                id="early_handoff", call_id="early_handoff", type="function_call",
                name=kwargs["handoffs"][0].tool_name, arguments="{}",
            )], usage=Usage(requests=1), response_id=None)
        return await super().get_response(system_instructions, input, **kwargs)


def test_source_only_early_handoff_model_collects_real_execution_records_first():
    model, events = GateAwareModel(), []
    report = asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert model.requests[0]["model_settings"].tool_choice == "read_execution_records"
    assert model.requests[1]["model_settings"].tool_choice == "read_source_file"
    for request, name in zip(model.requests, ("read_execution_records", "read_source_file")):
        assert [tool.name for tool in request["tools"]] == [name]
        assert request["handoffs"] == []
        assert request["model_settings"].parallel_tool_calls is False
    assert report["execution_read"] is True
    assert report["files_read"] == ["app.py"]
    assert len(report["handoffs"]) == 3
    kinds = [kind for kind, _ in events]
    assert kinds.index("evidence") < kinds.index("source") < kinds.index("handoff")
    assert events.count(("evidence", "execution_records")) == 1
    assert model.calls == 6


@pytest.mark.parametrize("missing,code", [
    ("execution_read", "analysis_execution_unread"),
    ("files_read", "analysis_source_unread"),
    ("handoffs", "analysis_handoff_incomplete"),
])
def test_report_guard_identifies_the_actual_missing_prerequisite(missing, code):
    report = asyncio.run(run_analysis(ReviewModel(findings=False), context()))
    report[missing] = False if missing == "execution_read" else []
    with pytest.raises(ValueError, match=code):
        validate_report(report, context())
    assert failure_code(ValueError(code)) == code


@pytest.mark.parametrize("which", ["read_execution_records", "read_source_file"])
def test_ignoring_required_tool_fails_before_handoff_or_execution(which):
    class IgnoringModel(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if kwargs["model_settings"].tool_choice == which:
                self.calls += 1
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id="forged", call_id="forged", type="function_call",
                    name="transfer_to_problem_investigator", arguments="{}",
                )], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = IgnoringModel(), []
    code = ("analysis_execution_read_not_followed" if which == "read_execution_records"
            else "analysis_source_read_not_followed")
    with pytest.raises(AnalysisFailure, match=code):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.calls == (1 if which == "read_execution_records" else 2)
    assert not any(kind == "source" for kind, _ in events)


def test_missing_source_reads_do_not_authorize_handoff_without_a_quota():
    class InvalidSource(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if self.calls >= 12:
                raise AnalysisFailure("analysis_source_unread")  # Fixture termination.
            result = await super().get_response(system_instructions, input, **kwargs)
            for output in result.output:
                if output.type == "function_call" and output.name == "read_source_file":
                    output.arguments = json.dumps({"path": "missing.py", "start_line": 1,
                                                   "end_line": 1})
            return result

    model, events = InvalidSource(), []
    with pytest.raises(AnalysisFailure, match="analysis_source_unread"):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert model.calls == 12
    assert not any(kind in ("handoff", "source") for kind, _ in events)


def test_explicit_closing_with_missing_execution_never_calls_provider():
    stage = StageModel(
        ReviewModel(), ROLES[0], StageBudget(started=time.monotonic() - 500), lambda *_: None,
        required_read=lambda: "read_execution_records",
        force_closing=True,
    )
    with pytest.raises(AnalysisFailure, match="analysis_execution_unread"):
        asyncio.run(stage.get_response("fixture", "fixture", tools=[],
                    handoffs=[SimpleNamespace(tool_name="transfer_to_problem_investigator")],
                    model_settings=ModelSettings()))
    assert stage.delegate.calls == 0


def test_mixed_read_and_handoff_is_rejected_without_executing_either():
    class MixedModel(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            result = await super().get_response(system_instructions, input, **kwargs)
            if self.calls == 1:
                result.output.append(ResponseFunctionToolCall(
                    id="handoff", call_id="handoff", type="function_call",
                    name="transfer_to_problem_investigator", arguments="{}",
                ))
            return result

    events = []
    with pytest.raises(AnalysisFailure, match="analysis_execution_read_not_followed"):
        asyncio.run(run_analysis(MixedModel(), context(), lambda *args: events.append(args)))
    assert not any(kind in ("handoff", "source", "evidence") for kind, _ in events)


def test_durable_failed_execution_read_has_specific_error_and_no_findings(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)

    class BadEngine(Engine):
        async def analyze(self, context, event):
            data = await run_analysis(GateAwareModel(), context, event)
            data["execution_read"] = False
            return data

    analyses = Analyses(execution, BadEngine())
    asyncio.run(analyses.tick())
    failed = analyses.list(env["id"])[0]
    assert failed["status"] == "failed" and failed["error"] == "analysis_execution_unread"
    assert failed["report"] is None and failed["findings"] == []
    assert failed["failure_details"]["stage"] == ROLES[-1]


@pytest.mark.parametrize("count", [1, 2, 10])
def test_required_source_accepts_bounded_pure_read_batches(count):
    class BatchModel(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            result = await super().get_response(system_instructions, input, **kwargs)
            if kwargs["model_settings"].tool_choice == "read_source_file":
                original = result.output[0]
                result.output = [original.model_copy(update={
                    "id": f"batch_{index}", "call_id": f"batch_{index}",
                    "arguments": json.dumps({"path": "app.py" if index == 0 else "other.py",
                                              "start_line": 1, "end_line": 1}),
                }) for index in range(count)]
            return result

    model, events = BatchModel(), []
    ctx = context()
    ctx["files"]["other.py"] = "print('other')"
    result = asyncio.run(run_analysis(model, ctx, lambda *args: events.append(args)))
    assert result["execution_read"] is True and len(result["handoffs"]) == 3
    assert result["files_read"] == (["app.py"] if count == 1 else ["app.py", "other.py"])
    assert sum(kind == "source" for kind, _ in events) == count
    checks = [json.loads(value) for kind, value in events if kind == "required_read_validation"]
    assert checks[-1] == {"required": "source", "calls": count, "matching": count,
                         "other": 0, "allowed": count, "messages": 0, "refusals": 0,
                         "text_chars": 0, "accepted": True, "reason": "accepted",
                         "recoverable": False, "recovery": "not_needed",
                         "corrections_remaining": 2}
    assert model.calls == 6


@pytest.mark.parametrize("case", ["wrong_tool", "too_many", "empty", "duplicate_execution"])
def test_required_batch_rejection_is_pre_execution_and_diagnostics_do_not_leak(case):
    class BadBatch(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            result = await super().get_response(system_instructions, input, **kwargs)
            target = ("read_execution_records" if case == "duplicate_execution"
                      else "read_source_file")
            if kwargs["model_settings"].tool_choice == target:
                original = result.output[0]
                if case == "empty":
                    result.output = []
                else:
                    count = 11 if case == "too_many" else 2
                    result.output = [original.model_copy(update={
                        "id": f"bad_{index}", "call_id": f"bad_{index}",
                    }) for index in range(count)]
                    if case == "wrong_tool":
                        result.output[-1].name = "unknown_sensitive_tool_name"
                        result.output[-1].arguments = '{"private":"credential-sentinel"}'
            return result

    model, events = BadBatch(), []
    code = ("analysis_execution_read_not_followed" if case == "duplicate_execution"
            else "analysis_tool_arguments_exhausted" if case == "empty"
            else "analysis_source_read_not_followed")
    if case == "too_many":
        report = asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
        assert len(report["handoffs"]) == 3
        assert sum(kind == "source" for kind, _ in events) == 11
        return
    def record(kind, value):
        events.append((kind, value))
    with pytest.raises(AnalysisFailure, match=code):
        asyncio.run(run_analysis(model, context(), record))
    assert not any(kind in ("source", "handoff") for kind, _ in events)
    checks = [json.loads(value) for kind, value in events if kind == "required_read_validation"]
    assert checks[-1]["calls"] == (0 if case == "empty" else 11 if case == "too_many" else 2)
    if case == "wrong_tool":
        assert checks[-1]["other"] == 1 and checks[-1]["matching"] == 1
    assert "credential-sentinel" not in str(events)
    assert "unknown_sensitive_tool_name" not in str(events)
    assert model.calls == (1 if case == "duplicate_execution" else 4 if case == "empty" else 2)


def test_scope_wrong_tool_gets_one_sanitized_correction_then_succeeds():
    class WrongScopeOnce(ReviewModel):
        def __init__(self):
            super().__init__(findings=False)
            self.wrong_scope_calls = 0

        async def get_response(self, system_instructions, input, **kwargs):
            if (kwargs["model_settings"].tool_choice == "register_task_scope"
                    and self.wrong_scope_calls == 0):
                self.wrong_scope_calls += 1
                self.calls += 1
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id="scope_wrong",
                    call_id="scope_wrong",
                    type="function_call",
                    name="private_wrong_tool_name",
                    arguments='{"private":"credential-sentinel"}',
                )], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = WrongScopeOnce(), []
    report = asyncio.run(run_analysis(model, scope_context(), record_scope(events)))

    checks = [
        json.loads(value) for kind, value in events if kind == "required_read_validation"
    ]
    scope_check = next(check for check in checks if check["required"] == "scope")
    assert scope_check["reason"] == "wrong_tool"
    assert scope_check["recoverable"] is True
    assert scope_check["recovery"] == "retry"
    assert [value for kind, value in events if kind == "required_read_retry"] == ["scope"]
    assert [value for kind, value in events if kind == "required_read_recovered"] == ["scope"]
    assert len(report["handoffs"]) == 3
    assert model.calls == 8
    serialized = json.dumps(events)
    assert "private_wrong_tool_name" not in serialized
    assert "credential-sentinel" not in serialized


def test_scope_wrong_tool_second_failure_still_fails_closed_without_leak():
    class WrongScopeAlways(ReviewModel):
        def __init__(self):
            super().__init__(findings=False)
            self.scope_attempts = 0

        async def get_response(self, system_instructions, input, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                self.scope_attempts += 1
                self.calls += 1
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id=f"scope_wrong_{self.calls}",
                    call_id=f"scope_wrong_{self.calls}",
                    type="function_call",
                    name="private_wrong_tool_name",
                    arguments='{"private":"credential-sentinel"}',
                )], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = WrongScopeAlways(), []
    with pytest.raises(AnalysisFailure, match="analysis_scope_read_not_followed"):
        asyncio.run(run_analysis(
            model, scope_context(), lambda *args: events.append(args),
        ))

    checks = [
        json.loads(value) for kind, value in events if kind == "required_read_validation"
    ]
    assert [check["recoverable"] for check in checks if check["required"] == "scope"] == [
        True, False,
    ]
    assert len([kind for kind, _ in events if kind == "required_read_retry"]) == 1
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.scope_attempts == 2
    serialized = json.dumps(events)
    assert "private_wrong_tool_name" not in serialized
    assert "credential-sentinel" not in serialized


def test_source_pure_wrong_tool_stays_non_recoverable():
    class WrongSource(ReviewModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if kwargs["model_settings"].tool_choice == "read_source_file":
                self.calls += 1
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id="source_wrong",
                    call_id="source_wrong",
                    type="function_call",
                    name="private_wrong_tool_name",
                    arguments='{"private":"credential-sentinel"}',
                )], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = WrongSource(), []
    with pytest.raises(AnalysisFailure, match="analysis_source_read_not_followed"):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))

    checks = [
        json.loads(value) for kind, value in events if kind == "required_read_validation"
    ]
    source_check = next(check for check in checks if check["required"] == "source")
    assert source_check["recoverable"] is False
    assert source_check["recovery"] == "request_mismatch"
    assert not any(kind == "required_read_retry" for kind, _ in events)
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.calls == 2
    serialized = json.dumps(events)
    assert "private_wrong_tool_name" not in serialized
    assert "credential-sentinel" not in serialized


def test_scope_mixed_correct_and_wrong_tool_stays_non_recoverable():
    class MixedScope(ReviewModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                self.calls += 1
                return ModelResponse(output=[
                    ResponseFunctionToolCall(
                        id="scope_correct",
                        call_id="scope_correct",
                        type="function_call",
                        name="register_task_scope",
                        arguments=json.dumps({"items": [{
                            "key": "fixture_behavior",
                            "title": "Fixture behavior",
                            "expected": "Preserve the original fixture behavior",
                            "paths": ["app.py"],
                            "required_evidence": ["regression"],
                        }]}),
                    ),
                    ResponseFunctionToolCall(
                        id="scope_wrong",
                        call_id="scope_wrong",
                        type="function_call",
                        name="private_wrong_tool_name",
                        arguments='{"private":"credential-sentinel"}',
                    ),
                ], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = MixedScope(), []
    with pytest.raises(AnalysisFailure, match="analysis_scope_read_not_followed"):
        asyncio.run(run_analysis(
            model, scope_context(), lambda *args: events.append(args),
        ))

    checks = [
        json.loads(value) for kind, value in events if kind == "required_read_validation"
    ]
    scope_check = next(check for check in checks if check["required"] == "scope")
    assert scope_check["matching"] == 1 and scope_check["other"] == 1
    assert scope_check["recoverable"] is False
    assert scope_check["recovery"] == "request_mismatch"
    assert not any(kind == "required_read_retry" for kind, _ in events)
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.calls == 3
    serialized = json.dumps(events)
    assert "private_wrong_tool_name" not in serialized
    assert "credential-sentinel" not in serialized


@pytest.mark.parametrize("case", ["too_many", "refusal"])
def test_scope_non_pure_batches_do_not_trigger_wrong_tool_correction(case):
    class BadScope(ReviewModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                self.calls += 1
                if case == "refusal":
                    return ModelResponse(output=[ResponseOutputMessage(
                        id="scope_refusal",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[ResponseOutputRefusal(
                            type="refusal", refusal="Fixture provider refusal.",
                        )],
                    )], usage=Usage(requests=1), response_id=None)
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id=f"scope_{index}",
                    call_id=f"scope_{index}",
                    type="function_call",
                    name="register_task_scope",
                    arguments=json.dumps({"items": [{
                        "key": f"fixture_{index}",
                        "title": "Fixture behavior",
                        "expected": "Preserve the original fixture behavior",
                        "paths": ["app.py"],
                        "required_evidence": ["regression"],
                    }]}),
                ) for index in range(3)], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model, events = BadScope(), []
    with pytest.raises(Exception):
        asyncio.run(run_analysis(
            model, scope_context(), lambda *args: events.append(args),
        ))

    checks = [
        json.loads(value) for kind, value in events if kind == "required_read_validation"
    ]
    scope_check = next(check for check in checks if check["required"] == "scope")
    assert scope_check["recoverable"] is False
    assert scope_check["recovery"] in {"request_mismatch", "refusal"}
    assert not any(kind == "required_read_retry" for kind, _ in events)
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.calls == 3


def test_invalid_source_batch_still_consumes_original_read_budget():
    class InvalidBatch(GateAwareModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if self.calls >= 5:
                raise AnalysisFailure("analysis_source_unread")  # Fixture stop.
            result = await super().get_response(system_instructions, input, **kwargs)
            if kwargs["model_settings"].tool_choice == "read_source_file":
                original = result.output[0]
                result.output = [original.model_copy(update={
                    "id": f"invalid_{index}", "call_id": f"invalid_{index}",
                    "arguments": json.dumps({"path": "missing.py", "start_line": 1,
                                              "end_line": 1}),
                }) for index in range(10)]
            return result

    model, events = InvalidBatch(), []
    with pytest.raises(AnalysisFailure, match="analysis_source_unread"):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert model.calls == 5
    assert not any(kind in ("source", "handoff") for kind, _ in events)
