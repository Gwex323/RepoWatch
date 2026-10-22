import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agents import (
    AgentOutputSchema,
    Model,
    ModelResponse,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Usage,
    function_tool,
)
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from fastapi.testclient import TestClient
from openai import APITimeoutError, AsyncOpenAI, BadRequestError
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from test_execution import Runtime, Source, prepare, service  # noqa: F401
from test_github_accounts import TOKEN_A, MemoryVault, Remote

from repowatch.analysis_model import (
    REQUEST_TIMEOUT,
    AnalysisModel,
    JsonObjectChatModel,
    ObservedModel,
    OutputTruncated,
    effective_reasoning,
    finalization_mode,
    observe_response,
    output_protocol,
    separate_review_schema_from_tools,
)
from repowatch.app import create_app
from repowatch.repository_analysis import (
    ANALYSIS_TIMEOUT_SECONDS,
    MAX_TURNS,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_DELAY_SECONDS,
    ROLES,
    STAGE_LIMITS,
    STAGE_SECONDS,
    Analyses,
    AnalysisFailure,
    Review,
    StageBudget,
    StageModel,
    evidence_issues,
    failure_code,
    final_report_input,
    normalize_evidence_quotes,
    report_diagnostics,
    run_analysis,
    source_snapshot,
    validate_report,
)
from repowatch.store import Conflict


class ReviewModel(Model):
    def __init__(self, *, invented=False, skip_review=False, findings=True):
        self.calls = 0
        self.invented, self.skip_review, self.findings = invented, skip_review, findings

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        identity = f"analysis_call_{self.calls}"
        if self.calls == 1:
            name, args = "read_execution_records", {}
        elif self.calls == 2:
            name, args = "read_source_file", {"path": "app.py", "start_line": 1, "end_line": 3}
        elif kwargs["handoffs"] and not self.skip_review:
            name, args = kwargs["handoffs"][0].tool_name, {}
        else:
            data = {
                "summary": "Deterministic SDK integration fixture, not a real repository finding.",
                "limitations": ["Test model only."],
                "findings": [{
                    "title": "Fixture suspected behavior", "severity": "P2",
                    "explanation": "Fixture explanation", "expected": "Fixture expected",
                    "suspected_actual": "Fixture actual", "reproduction": ["Operate fixture."],
                    "evidence": [{"path": "app.py", "start_line": 1, "end_line": 1,
                                  "quote": "invented quote" if self.invented else "print('ok')"}],
                }] if self.findings else [],
            }
            return ModelResponse(output=[ResponseOutputMessage(
                id=identity, type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=json.dumps(data),
                                            annotations=[])],
            )], usage=Usage(requests=1), response_id=None)
        return ModelResponse(output=[ResponseFunctionToolCall(
            id=identity, call_id=identity, type="function_call",
            name=name, arguments=json.dumps(args),
        )], usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError()
        yield


class Engine:
    enabled = True

    def status(self):
        return {"enabled": self.enabled, "model": "test-only"}

    async def analyze(self, context, event):
        return await run_analysis(ReviewModel(), context, event)


class BrowsingModel(ReviewModel):
    """Keep browsing until the orchestration layer forces the closing call."""

    def __init__(self, *, ignore_choice=False, burst=1, oversized=False):
        super().__init__(findings=False)
        self.ignore_choice, self.burst, self.oversized = ignore_choice, burst, oversized
        self.ignore_final = False
        self.requests = []

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        choice = kwargs["model_settings"].tool_choice
        identity = f"browse_{self.calls}"
        if choice == "none" and not self.ignore_choice and not self.ignore_final:
            return ModelResponse(output=[ResponseOutputMessage(
                id=identity, type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=json.dumps({
                    "summary": "Bounded SDK review fixture.", "limitations": ["Limited coverage."],
                    "findings": [],
                }), annotations=[])],
            )], usage=Usage(requests=1), response_id=None)
        if choice and not self.ignore_choice and not (choice == "none" and self.ignore_final):
            if choice == "read_source_file":
                calls = [(choice, {"path": "large.py" if self.oversized else "app.py",
                                   "start_line": 1, "end_line": 120})]
            elif choice == "read_execution_records":
                calls = [(choice, {})]
            else:
                assert not kwargs["tools"]
                calls = [(choice, {})]
        elif self.calls == 1:
            calls = [("read_execution_records", {})]
        else:
            calls = [("read_source_file", {
                "path": "large.py" if self.oversized else "app.py",
                "start_line": 1, "end_line": 120,
            })] * self.burst
        return ModelResponse(output=[
            ResponseFunctionToolCall(
                id=f"{identity}_{index}", call_id=f"{identity}_{index}", type="function_call",
                name=name, arguments=json.dumps(args),
            ) for index, (name, args) in enumerate(calls)
        ], usage=Usage(requests=1), response_id=None)


TEXTUAL_TOOL_CALL = (
    "<tool_call>read_source_file<arg_key>path</arg_key>"
    "<arg_value>app.py</arg_value></tool_call>"
)


@pytest.mark.parametrize("adapter,invalid,fast", [
    (OpenAIChatCompletionsModel, None, False),
    (JsonObjectChatModel, '{"summary":"Missing fields."}', False),
    (JsonObjectChatModel, TEXTUAL_TOOL_CALL, False),
    (JsonObjectChatModel, TEXTUAL_TOOL_CALL, True),
])
def test_chat_adapter_truncation_recovers_before_executing_tools_and_keeps_handoffs(
    adapter, invalid, fast,
):
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events, limits = [], []
    observation, successful = {}, {"calls": 0}

    async def transport(request):
        payload = json.loads(request.content)
        assert payload.get("reasoning_effort") == (None if fast else "low")
        limits.append(payload["max_tokens"])
        if len(limits) == 1:
            message, reason = {"role": "assistant", "content": None}, "length"
        else:
            successful["calls"] += 1
            turn = successful["calls"]
            if turn == 1:
                name, args = "read_execution_records", {}
            elif turn == 2:
                name, args = "read_source_file", {"path": "app.py", "start_line": 1, "end_line": 1}
            elif turn <= 5:
                name = next(tool["function"]["name"] for tool in payload["tools"]
                            if tool["function"]["name"].startswith("transfer_to_"))
                args = {}
            else:
                name = None
            if turn <= 2:
                assert payload["tool_choice"] == {
                    "type": "function", "function": {"name": name},
                }
                assert [tool["function"]["name"] for tool in payload["tools"]] == [name]
            if name:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call_{turn}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}
                reason = "tool_calls"
            else:
                expected_format = "json_object" if adapter is JsonObjectChatModel else "json_schema"
                assert payload["response_format"]["type"] == expected_format
                content = json.dumps({
                    "summary": "Mock provider review.", "limitations": [], "findings": [],
                })
                if invalid is not None and turn == 6:
                    content = invalid
                if invalid is not None and turn == 7:
                    assert [message["role"] for message in payload["messages"]] == [
                        "system", "user",
                    ]
                    packet = json.loads(payload["messages"][1]["content"])
                    assert packet["execution_was_read"] is True
                    assert packet["source_evidence"][0]["source_blocks"] == [
                        {"line": 1, "text": "print('ok')"},
                    ]
                    assert TEXTUAL_TOOL_CALL not in payload["messages"][1]["content"]
                    assert (payload.get("thinking") == {"type": "disabled"}) is fast
                message = {"role": "assistant", "content": content}
                reason = "stop"
        return httpx.Response(200, json={
            "id": "completion_mock", "object": "chat.completion", "created": 1,
            "model": "mock-provider", "choices": [{
                "index": 0, "message": message, "finish_reason": reason,
            }], "usage": {"prompt_tokens": 100, "completion_tokens": 3000,
                         "total_tokens": 3100,
                         "completion_tokens_details": {"reasoning_tokens": 2999}},
        })

    def record(*item):
        events.append(item)

    async def observe(response):
        await observe_response(response, observation, record)

    async def run():
        async with AsyncOpenAI(
            api_key="mock-not-a-real-key", base_url="https://fixture.example/v1", max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(transport), event_hooks={"response": [observe]},
            ),
        ) as client:
            delegate = (
                adapter("mock-provider", client, fast_finalization=fast, event=record)
                if adapter is JsonObjectChatModel else adapter("mock-provider", client)
            )
            model = ObservedModel(delegate, observation)
            return await run_analysis(
                model, context, record, reasoning_effort=None if fast else "low",
            )

    report = asyncio.run(run())
    assert limits[:2] == [3000, 6000]
    assert len(limits) == (8 if adapter is JsonObjectChatModel else 7)
    assert len(report["handoffs"]) == 3
    assert sum(kind == "source" for kind, _ in events) == 1
    assert sum(kind == "output_retry" for kind, _ in events) == 1
    assert sum(kind == "format_retry" for kind, _ in events) == (
        1 if adapter is JsonObjectChatModel else 0
    )
    assert sum(kind == "finalization_thinking" for kind, _ in events) == int(fast)
    metadata = [json.loads(value) for kind, value in events if kind == "provider_response"]
    assert metadata[0] == {
        "finish_reason": "length", "truncated": True, "output_tokens": 3000,
        "reasoning_tokens": 2999,
    }


def test_reasoning_defaults_are_targeted_and_owner_can_restore_provider_default():
    assert effective_reasoning({"model": "glm-5.3"}) == "low"
    assert effective_reasoning({"model": "GLM-5.3"}) == "low"
    assert effective_reasoning({"model": "glm-5.3-flash"}) == "low"
    assert effective_reasoning({"model": "GLM-5.3-FLASH", "reasoning_effort": "auto"}) == "low"
    assert effective_reasoning({"model": "glm-5.3-flash", "reasoning_effort": "default"}) is None
    assert effective_reasoning({"model": "glm-5.3-flash", "reasoning_effort": "high"}) == "high"
    assert effective_reasoning({"model": "other-provider-model"}) is None
    assert effective_reasoning({"model": "glm-5.3", "reasoning_effort": "default"}) is None
    assert effective_reasoning({"model": "glm-5.3", "reasoning_effort": "high"}) == "high"


@pytest.mark.parametrize("model,host,api,effort,expected", [
    ("glm-4.7", "open.bigmodel.cn", "chat_completions", None, "glm47_direct"),
    ("GLM-4.7", "api.z.ai", "chat_completions", "auto", "glm47_direct"),
    ("glm-4.7", "open.bigmodel.cn", "chat_completions", "default", "provider_default"),
    ("glm-4.7", "open.bigmodel.cn", "chat_completions", "high", "provider_default"),
    ("glm-4.7", "gateway.example", "chat_completions", "auto", "provider_default"),
    ("glm-4.7", "open.bigmodel.cn", "responses", "auto", "provider_default"),
    ("glm-5.3", "open.bigmodel.cn", "chat_completions", "auto", "provider_default"),
    ("glm-4.7-flash", "open.bigmodel.cn", "chat_completions", "auto", "provider_default"),
])
def test_direct_finalization_is_narrow_and_preserves_owner_override(
    model, host, api, effort, expected,
):
    config = {"model": model, "base_url": f"https://{host}/v1", "api": api}
    if effort is not None:
        config["reasoning_effort"] = effort
    assert finalization_mode(config) == expected


@pytest.mark.parametrize("case", [
    "final", "explore", "tools_present", "plain", "explicit_default", "explicit_reasoning",
    "explicit_thinking", "other_extra_body",
])
def test_direct_finalization_thinking_is_sent_only_for_no_tools_report_requests(case):
    from agents.models.interface import ModelTracing
    from openai.types.shared import Reasoning

    @function_tool
    def synthetic_read() -> str:
        """Return synthetic evidence without file access."""
        return "synthetic"

    settings = ModelSettings(max_tokens=5000, tool_choice="none")
    if case == "explicit_reasoning":
        settings.reasoning = Reasoning(effort="high")
    elif case == "explicit_thinking":
        settings.extra_body = {"thinking": {"type": "enabled"}}
    elif case == "other_extra_body":
        settings.extra_body = {"synthetic_flag": True}
    elif case == "explore":
        settings.tool_choice = None
    before = settings.to_json_dict()
    events, requests = [], []

    async def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if case == "plain":
            assert "response_format" not in payload
        else:
            assert payload["response_format"]["type"] == "json_object"
        disabled = case in ("final", "other_extra_body")
        assert (payload.get("thinking") == {"type": "disabled"}) is disabled
        if case == "explicit_thinking":
            assert payload["thinking"] == {"type": "enabled"}
        if case == "other_extra_body":
            assert payload["synthetic_flag"] is True
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": "glm-4.7",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant",
                "content": '{"summary":"Synthetic review","limitations":[],"findings":[]}',
            }}],
        })

    async def run():
        async with AsyncOpenAI(
            api_key="synthetic-not-a-real-key", base_url="https://fixture.example/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        ) as client:
            model = JsonObjectChatModel(
                "glm-4.7", client, fast_finalization=case != "explicit_default",
                event=lambda *item: events.append(item),
            )
            return await model.get_response(
                system_instructions="Synthetic JSON only", input="Synthetic evidence",
                model_settings=settings, tools=[synthetic_read] if case == "tools_present" else [],
                handoffs=[], output_schema=None if case == "plain" else AgentOutputSchema(Review),
                tracing=ModelTracing.DISABLED,
            )

    asyncio.run(run())
    assert len(requests) == 1
    assert settings.to_json_dict() == before
    assert events == ([("finalization_thinking", "disabled")] if case in (
        "final", "other_extra_body",
    ) else [])


@pytest.mark.parametrize("stage_age,remaining", [(95, 590), (0, 200)])
def test_time_budget_forces_native_handoff_before_exploration_exhausts(stage_age, remaining):
    events = []
    budget = StageBudget(started=time.monotonic() - stage_age)
    model = BrowsingModel()
    stage = StageModel(
        model, ROLES[0], budget, lambda *item: events.append(item),
        deadline=time.monotonic() + remaining,
    )
    response = asyncio.run(stage.get_response(
        "Fixture", "Fixture",
        model_settings=ModelSettings(max_tokens=3000), tools=[object()],
        handoffs=[SimpleNamespace(tool_name="transfer_to_problem_investigator")],
    ))
    assert response.output[0].name == "transfer_to_problem_investigator"
    assert ("time_budget", ROLES[0]) in events
    assert budget.turns == 1
    assert model.requests[0]["tools"] == []


def test_expired_stage_deadline_never_calls_provider():
    model = BrowsingModel()
    stage = StageModel(
        model, ROLES[3], StageBudget(), lambda *_: None,
        deadline=time.monotonic() - 1,
    )
    with pytest.raises(AnalysisFailure, match="analysis_deadline_exceeded"):
        asyncio.run(stage.get_response(
            "Fixture", "Fixture", model_settings=ModelSettings(max_tokens=5000),
            tools=[], handoffs=[],
        ))
    assert model.calls == 0


@pytest.mark.parametrize("body", [
    {"choices": [{"finish_reason": "length", "message": {"content": "private-secret"}}]},
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
     "output": [{"text": "private-secret"}]},
])
def test_truncation_metadata_is_secret_free_and_rejects_partial_output(body):
    observation, events = {}, []
    response = httpx.Response(200, json=body)
    asyncio.run(observe_response(response, observation, lambda *item: events.append(item)))
    assert observation["truncated"]
    assert "private-secret" not in json.dumps(events)
    model = ReviewModel()

    async def response_with_metadata(*args, **kwargs):
        observation["truncated"] = True
        return "partial-result-must-not-be-accepted"

    model.get_response = response_with_metadata
    with pytest.raises(OutputTruncated):
        asyncio.run(ObservedModel(model, observation).get_response())


@pytest.mark.parametrize("error,expected", [
    ({"code": "InvalidParameter", "param": "response_format.json_schema",
      "message": "private-secret"},
     {"http_status": 400, "provider_code": "InvalidParameter",
      "parameter": "response_format"}),
    ({"code": "private-secret", "param": "private-secret",
      "message": "private-secret"}, {"http_status": 400}),
])
def test_provider_rejection_records_only_allowlisted_diagnostics(error, expected):
    events = []
    response = httpx.Response(400, json={"error": error})
    asyncio.run(observe_response(response, {}, lambda *item: events.append(item)))
    assert events == [("provider_rejection", json.dumps(expected))]
    assert "private-secret" not in json.dumps(events)


def test_reviewer_sends_tools_and_report_schema_in_separate_requests():
    from agents.models.interface import ModelTracing

    @function_tool
    def synthetic_read() -> str:
        """Return synthetic source evidence."""
        return "synthetic"

    payloads = []

    async def transport(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            assert [tool["function"]["name"] for tool in payload["tools"]] == [
                "synthetic_read",
            ]
            assert "response_format" not in payload
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "synthetic_read", "arguments": "{}"},
            }]}
            reason = "tool_calls"
        else:
            assert not payload.get("tools")
            assert payload["tool_choice"] == "none"
            assert payload["response_format"]["type"] == "json_schema"
            message = {"role": "assistant", "content": json.dumps({
                "summary": "Synthetic review", "limitations": [], "findings": [],
            })}
            reason = "stop"
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "message": message,
                                            "finish_reason": reason}],
        })

    async def run():
        async with AsyncOpenAI(
            api_key="synthetic-not-a-real-key", base_url="https://fixture.example/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        ) as client:
            stage = StageModel(
                OpenAIChatCompletionsModel("fixture", client), ROLES[-1],
                StageBudget(), lambda *_: None, separate_schema_from_tools=True,
            )
            request = dict(
                system_instructions="Synthetic only", input="Synthetic evidence",
                model_settings=ModelSettings(max_tokens=5000), tools=[synthetic_read],
                handoffs=[], output_schema=AgentOutputSchema(Review),
                tracing=ModelTracing.DISABLED,
            )
            first = await stage.get_response(**request)
            assert first.output[0].type == "function_call"
            stage.force_closing = True
            second = await stage.get_response(**request)
            assert second.output[0].type == "message"

    asyncio.run(run())
    assert len(payloads) == 2


def test_repeated_truncation_is_bounded_and_never_reaches_tools():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, limits, events = ReviewModel(), [], []

    async def truncated(*args, **kwargs):
        limits.append(kwargs["model_settings"].max_tokens)
        raise OutputTruncated("analysis_output_truncated")

    model.get_response = truncated
    with pytest.raises(AnalysisFailure, match="analysis_output_truncated"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert limits == [3000, 6000]
    assert not any(kind == "source" for kind, _ in events)
    assert not any(kind == "handoff" for kind, _ in events)


def test_output_retry_budget_is_shared_and_does_not_retry_third_truncation():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events, attempts = ReviewModel(), [], []
    original = model.get_response

    async def sometimes_truncated(*args, **kwargs):
        attempts.append(kwargs["model_settings"].max_tokens)
        if len(attempts) in (1, 5, 7):
            raise OutputTruncated("analysis_output_truncated")
        return await original(*args, **kwargs)

    model.get_response = sometimes_truncated
    with pytest.raises(AnalysisFailure, match="analysis_output_truncated"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(attempts) == 7
    assert sum(kind == "output_retry" for kind, _ in events) == 2


def test_output_retry_cannot_exceed_token_cap(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.MAX_OUTPUT_TOKENS", 3000)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events = ReviewModel(), []

    async def truncated(*args, **kwargs):
        raise OutputTruncated("analysis_output_truncated")

    model.get_response = truncated
    with pytest.raises(AnalysisFailure, match="analysis_output_truncated"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert not any(kind == "output_retry" for kind, _ in events)


def test_specific_model_behavior_failures_are_classified_without_payloads():
    for message, code in [
        ("ChatCompletion response has no choices: private-secret",
         "analysis_provider_empty_response"),
        ("Invalid JSON input for tool read_source_file: private-secret",
         "analysis_tool_arguments_invalid"),
        ("Other provider error private-secret", "analysis_model_output_invalid"),
    ]:
        assert failure_code(ModelBehaviorError(message)) == code


def final_message(text):
    return ModelResponse(output=[ResponseOutputMessage(
        id="final_fixture", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )], usage=Usage(requests=1), response_id=None)


def evidence_review(quote, *, start=1, end=1, path="app.py"):
    return Review.model_validate({
        "summary": "Synthetic report only", "limitations": [], "findings": [{
            "title": "Synthetic finding", "severity": "P2", "explanation": "Synthetic",
            "expected": "Synthetic expected", "suspected_actual": "Synthetic actual",
            "reproduction": ["Operate the synthetic fixture."],
            "evidence": [{"path": path, "start_line": start, "end_line": end, "quote": quote}],
        }],
    })


@pytest.mark.parametrize("source,quote,end,expected,changed", [
    ("print('ok')", "1: print('ok')", 1, "print('ok')", 1),
    ("def f():\n    return 1", "1: def f():\n2:     return 1", 2,
     "def f():\n    return 1", 1),
    ("1: print('ok')", "1: print('ok')", 1, "1: print('ok')", 0),
    ("print('ok')", "1: invented()", 1, "1: invented()", 0),
    ("print('ok')", "2: print('ok')", 1, "2: print('ok')", 0),
    ("    return 1", "1: return 1", 1, "return 1", 1),
    ("return 1", "```python\nreturn 1\n```", 1, "```python\nreturn 1\n```", 0),
    ("return 1", "1: return 1", 10**12, "1: return 1", 0),
])
def test_evidence_normalization_only_strips_verified_display_decoration(
    source, quote, end, expected, changed,
):
    review = evidence_review(quote, end=end)
    context = {"files": {"app.py": source}}
    reads = {"app.py": [[1, len(source.splitlines())]]}
    assert normalize_evidence_quotes(review, context, {"app.py"}, reads) == changed
    assert review.findings[0].evidence[0].quote == expected
    if changed:
        assert evidence_issues(review, context, {"app.py"}, reads) == []


@pytest.mark.parametrize("ranges,reason", [
    ([[1, 1], [2, 3]], None),
    ([[3, 3], [1, 2]], None),
    ([[1, 1], [3, 3]], "unread_range"),
    ([[2, 3]], "unread_range"),
    ([], "unread_range"),
])
def test_evidence_range_accepts_contiguous_read_union_but_never_gaps(ranges, reason):
    source = "first\nsecond\nthird"
    review = evidence_review(source, end=3)
    issues = evidence_issues(review, {"files": {"app.py": source}}, {"app.py"}, {"app.py": ranges})
    assert issues == ([] if reason is None else [{"finding": 1, "evidence": 1, "reason": reason}])
    report = {**review.model_dump(), "files_read": ["app.py"], "read_ranges": {"app.py": ranges},
              "execution_read": True,
              "handoffs": [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])]}
    if reason is None:
        validate_report(report, {"files": {"app.py": source}})
    else:
        with pytest.raises(ValueError, match="analysis_evidence_unverified"):
            validate_report(report, {"files": {"app.py": source}})


@pytest.mark.parametrize("path,start,end,quote,read,reason", [
    ("missing.py", 1, 1, "private-secret", {"app.py"}, "unknown_file"),
    ("app.py", 1, 1, "private-secret", set(), "unread_file"),
    ("app.py", 1, 2000, "private-secret", {"app.py"}, "invalid_range"),
    ("app.py", 2, 1, "private-secret", {"app.py"}, "invalid_range"),
    ("app.py", 1, 1, "private-secret", {"app.py"}, "quote_mismatch"),
])
def test_evidence_diagnostics_only_include_indices_and_fixed_reasons(
    path, start, end, quote, read, reason,
):
    review = evidence_review(quote, start=start, end=end, path=path)
    issues = evidence_issues(review, {"files": {"app.py": "actual"}}, read, {"app.py": [[1, 1]]})
    assert issues == [{"finding": 1, "evidence": 1, "reason": reason}]
    assert "private-secret" not in json.dumps(issues)
    assert path not in json.dumps(issues)


def test_evidence_correction_does_not_replay_tools_or_handoffs():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events, attempts = ReviewModel(), [], []
    original = model.get_response

    async def wrong_then_correct(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            attempts.append(kwargs)
            if len(attempts) == 1:
                return final_message(evidence_review("private-secret").model_dump_json())
            assert kwargs["tools"] == [] and kwargs["handoffs"] == []
            assert kwargs["model_settings"].tool_choice == "none"
            assert "source_blocks" in kwargs["input"]
            assert "private-secret" not in str(args) + str(kwargs)
        return await original(*args, **kwargs)

    model.get_response = wrong_then_correct
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(attempts) == 2 and len(report["handoffs"]) == 3
    assert sum(kind == "source" for kind, _ in events) == 1
    assert sum(kind == "handoff" for kind, _ in events) == 3
    assert sum(kind == "evidence_retry" for kind, _ in events) == 1
    assert "private-secret" not in json.dumps(events)
    validate_report(report, context)


def test_numbered_evidence_is_canonicalized_before_sdk_output_and_persistence():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events = ReviewModel(), []
    original = model.get_response

    async def numbered(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            return final_message(evidence_review("1: print('ok')").model_dump_json())
        return await original(*args, **kwargs)

    model.get_response = numbered
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert report["findings"][0]["evidence"][0]["quote"] == "print('ok')"
    assert ("evidence_normalized", "1") in events
    assert not any(kind == "evidence_retry" for kind, _ in events)
    validate_report(report, context)


def test_repeated_forged_evidence_fails_after_one_correction():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        asyncio.run(run_analysis(ReviewModel(invented=True), context, lambda *e: events.append(e)))
    assert sum(kind == "evidence_retry" for kind, _ in events) == 1
    assert sum(kind == "source" for kind, _ in events) == 1


@pytest.mark.parametrize("outcome,code,calls", [
    ("tool", "analysis_finalization_not_followed", 2),
    ("deadline", "analysis_deadline_exceeded", 1),
    ("silent_empty", "analysis_evidence_unverified", 2),
    ("disclosed_empty", None, 2),
])
def test_evidence_correction_keeps_deadline_tool_and_disclosure_gates(outcome, code, calls):
    model, events, attempts = ReviewModel(), [], []
    stage = StageModel(
        model, ROLES[-1], StageBudget(), lambda *item: events.append(item),
        deadline=time.monotonic() + 120,
        review_evidence=lambda review: (
            [{"finding": 1, "evidence": 1, "reason": "quote_mismatch"}] if review.findings else []
        ),
    )

    async def respond(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            if outcome == "deadline":
                stage.deadline = time.monotonic() - 1
            return final_message(evidence_review("invented").model_dump_json())
        if outcome == "tool":
            return ModelResponse(output=[ResponseFunctionToolCall(
                id="unexpected", call_id="unexpected", type="function_call",
                name="read_source_file", arguments="{}",
            )], usage=Usage(requests=1), response_id=None)
        return final_message(json.dumps({
            "summary": "Synthetic correction",
            "limitations": (
                ["Unsupported finding excluded."] if outcome == "disclosed_empty" else []
            ),
            "findings": [],
        }))

    model.get_response = respond
    request = stage.get_response(
        "Synthetic instructions", "Synthetic source evidence",
        model_settings=ModelSettings(max_tokens=5000), output_schema=AgentOutputSchema(Review),
        tools=[], handoffs=[],
    )
    if code:
        with pytest.raises((AnalysisFailure, ValueError), match=code):
            asyncio.run(request)
    else:
        asyncio.run(request)
    assert len(attempts) == calls
    assert sum(kind == "evidence_retry" for kind, _ in events) == 1
    assert not any(kind in ("source", "handoff") for kind, _ in events)


@pytest.mark.parametrize("invalid", [
    "",
    "```json\n{}\n```",
    '{"path":"app.py","start_line":1,"end_line":1}',
    '{"summary":"private-secret","limitations":[],"findings":[],"unexpected":true}',
])
def test_final_report_format_correction_preserves_evidence_and_handoffs(invalid):
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events, requests = ReviewModel(), [], []
    original = model.get_response

    async def malformed_once(system_instructions, input, **kwargs):
        requests.append((system_instructions, input, kwargs))
        if len(requests) == 6:
            return final_message(invalid)
        return await original(system_instructions, input, **kwargs)

    model.get_response = malformed_once
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(requests) == 7 and len(report["handoffs"]) == 3
    assert sum(kind == "handoff" for kind, _ in events) == 3
    assert sum(kind == "source" for kind, _ in events) == 1
    assert sum(kind == "format_retry" for kind, _ in events) == 1
    assert report["findings"][0]["evidence"][0]["quote"] == "print('ok')"
    instructions, corrected_input, corrected = requests[-1]
    assert "Final report contract" in requests[-2][0]
    assert json.dumps(AgentOutputSchema(Review).json_schema()) in instructions
    assert "Correct the final report now" in instructions
    assert isinstance(corrected_input, str)
    packet = json.loads(corrected_input)
    assert packet["source_evidence"] == [{
        "path": "app.py", "ranges": [[1, 1]],
        "source_blocks": [{"line": 1, "text": "print('ok')"}],
    }]
    assert packet["execution_was_read"] is True
    assert packet["handoffs"] == report["handoffs"]
    assert "print('ok')" in corrected_input
    assert "private-secret" not in corrected_input
    assert corrected["tools"] == [] and corrected["handoffs"] == []
    assert corrected["model_settings"].tool_choice == "none"
    assert "private-secret" not in json.dumps(events)


def test_budget_closing_report_uses_only_actually_read_source_without_tool_history(monkeypatch):
    monkeypatch.setitem(STAGE_SECONDS, ROLES[-1], 0)
    context = {
        "files": {"app.py": "print('ok')\n# second\n# third\nUNREAD_SOURCE_SENTINEL"},
        "config": {}, "jobs": [], "test_result": None,
    }
    model, packets = ReviewModel(), []
    original = model.get_response

    async def closing(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            assert not kwargs["tools"]
            assert kwargs["model_settings"].tool_choice == "none"
            packets.append(json.loads(kwargs["input"]))
        response = await original(*args, **kwargs)
        if model.calls == 1:
            response.output.insert(
                0, final_message("Prior suspected behavior, not proof.").output[0],
            )
        return response

    model.get_response = closing
    report = asyncio.run(run_analysis(model, context))
    assert len(packets) == 1 and len(report["handoffs"]) == 3
    assert packets[0]["source_evidence"] == [{
        "path": "app.py", "ranges": [[1, 3]],
        "source_blocks": [{"line": 1, "text": "print('ok')"}, {"line": 2, "text": "# second"},
                          {"line": 3, "text": "# third"}],
    }]
    assert "UNREAD_SOURCE_SENTINEL" not in json.dumps(packets)
    assert packets[0]["prior_observations"] == ["Prior suspected behavior, not proof."]
    assert packets[0]["observations_clipped"] is False
    assert report["read_ranges"] == {"app.py": [[1, 3]]}


def test_report_correction_wraps_full_transcript_as_data_without_reasoning_or_truncation():
    content = "source evidence " * 6000
    transcript = [
        {"role": "user", "content": "repository context"},
        {"type": "reasoning", "summary": [{"text": "private reasoning"}]},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "prior observations"}
        ], "provider_data": {"private": "not evidence"}},
        {"type": "function_call", "name": "read_source_file", "arguments": '{"path":"app.py"}',
         "call_id": "evidence_call"},
        {"type": "function_call_output", "call_id": "evidence_call", "output": content},
    ]
    encoded = final_report_input(transcript)
    retained = json.loads(encoded)["evidence_transcript"]
    assert retained[0] == transcript[0]
    assert retained[1]["content"] == transcript[2]["content"]
    assert retained[2] == transcript[3]
    assert retained[3]["output"] == content
    assert "private reasoning" not in encoded and "provider_data" not in encoded
    assert json.loads(final_report_input("original context"))["evidence_transcript"] == (
        "original context"
    )


@pytest.mark.parametrize("text,category", [
    ("", "empty"), (" \n ", "empty"), ("secret invalid text", "invalid_json"),
    (TEXTUAL_TOOL_CALL, "textual_tool_call"),
    ('{"summary":"secret"}', "schema_mismatch"),
])
def test_report_diagnostics_are_counts_and_fixed_categories_only(text, category):
    diagnostic = json.loads(report_diagnostics(final_message(text)))
    assert diagnostic["text_chars"] == len(text)
    assert diagnostic["messages"] == 1
    assert diagnostic["category"] == category
    if category != "schema_mismatch":
        assert type(diagnostic["position"]) is int
        assert type(diagnostic["line"]) is int
        assert type(diagnostic["column"]) is int
    assert "secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("text,code", [
    ("private-secret: not JSON", "analysis_json_invalid"),
    ('{"summary":"private-secret"}', "analysis_report_invalid"),
])
def test_final_report_format_correction_is_bounded(text, code):
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events, attempts = ReviewModel(), [], []
    original = model.get_response

    async def invalid_final(system_instructions, input, **kwargs):
        if kwargs.get("output_schema") is not None:
            attempts.append(kwargs)
            return final_message(text)
        return await original(system_instructions, input, **kwargs)

    model.get_response = invalid_final
    with pytest.raises(AnalysisFailure, match=code):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(attempts) == 2
    assert sum(kind == "format_retry" for kind, _ in events) == 1
    assert "private-secret" not in json.dumps(events)


def test_correction_never_bypasses_source_evidence_checks():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, attempts = ReviewModel(invented=True), []
    original = model.get_response

    async def bad_then_invented(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            attempts.append(1)
            if len(attempts) == 1:
                return final_message("not JSON")
        return await original(*args, **kwargs)

    model.get_response = bad_then_invented
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        asyncio.run(run_analysis(model, context))
    assert len(attempts) == 3


def test_correction_cannot_restart_exploration():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, attempts, events = ReviewModel(), [], []
    original = model.get_response

    async def tool_after_invalid(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            attempts.append(1)
            if len(attempts) == 1:
                return final_message("not JSON")
            return ModelResponse(output=[ResponseFunctionToolCall(
                id="unexpected", call_id="unexpected", type="function_call",
                name="read_source_file", arguments='{"private-secret":true}',
            )], usage=Usage(requests=1), response_id=None)
        return await original(*args, **kwargs)

    model.get_response = tool_after_invalid
    with pytest.raises(AnalysisFailure, match="analysis_finalization_not_followed"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert sum(kind == "source" for kind, _ in events) == 1
    assert "private-secret" not in json.dumps(events)


def test_correction_respects_the_existing_deadline():
    model, events, attempts = ReviewModel(), [], []
    stage = StageModel(model, ROLES[-1], StageBudget(), lambda *item: events.append(item),
                       deadline=time.monotonic() + 100)

    async def invalid_at_deadline(*args, **kwargs):
        attempts.append(1)
        stage.deadline = time.monotonic() - 1
        return final_message("not JSON")

    model.get_response = invalid_at_deadline
    with pytest.raises(AnalysisFailure, match="analysis_deadline_exceeded"):
        asyncio.run(stage.get_response(
            "Fixture", "Fixture", model_settings=ModelSettings(max_tokens=5000),
            output_schema=AgentOutputSchema(Review), tools=[], handoffs=[],
        ))
    assert len(attempts) == 1


def test_budget_closing_report_can_be_corrected_without_replaying_exploration():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events, emitted = BrowsingModel(), [], []
    original = model.get_response

    async def bad_closing(*args, **kwargs):
        if kwargs["model_settings"].tool_choice == "none" and not emitted:
            emitted.append(True)
            return final_message("not JSON")
        return await original(*args, **kwargs)

    model.get_response = bad_closing
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(report["handoffs"]) == 3
    assert sum(kind == "format_retry" for kind, _ in events) == 1
    assert sum(item == ("model_call", ROLES[-1]) for item in events) == (
        STAGE_LIMITS[ROLES[-1]][0] + 1
    )


@pytest.mark.parametrize("host,model,api,expected", [
    ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7", "chat_completions", "json_object"),
    ("https://api.z.ai/api/paas/v4", "GLM-4.7", "chat_completions", "json_object"),
    ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7", "responses", "json_schema"),
    ("https://gateway.example/v1", "glm-4.7", "chat_completions", "json_schema"),
])
def test_json_mode_is_limited_to_known_glm_provider_contract(host, model, api, expected):
    assert output_protocol({"base_url": host, "model": model, "api": api}) == expected


@pytest.mark.parametrize("model,base_url,api,expected", [
    ("glm-5.3", "https://ark.cn-beijing.volces.com/api/coding/v3",
     "chat_completions", True),
    ("GLM-5.3-FLASH", "https://ark.cn-beijing.volces.com/api/coding/v3",
     "chat_completions", True),
    ("glm-5.3", "https://ark.cn-beijing.volces.com/api/v3",
     "chat_completions", False),
    ("glm-5.3", "https://ark.cn-beijing.volces.com/api/coding/v3",
     "responses", False),
    ("other-model", "https://ark.cn-beijing.volces.com/api/coding/v3",
     "chat_completions", False),
])
def test_schema_tool_separation_is_limited_to_ark_coding_glm53(
    model, base_url, api, expected,
):
    assert separate_review_schema_from_tools({
        "model": model, "base_url": base_url, "api": api,
    }) is expected


def test_ark_glm53_wires_schema_tool_separation(service, monkeypatch):  # noqa: F811
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(
        version=0, enabled=True, model="glm-5.3", api="chat_completions",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        api_key="synthetic-not-a-real-key",
    )

    async def synthetic_run(model, context, event, *, reasoning_effort):
        assert model.separate_schema_from_tools is True
        return {"summary": "Synthetic only", "limitations": [], "findings": []}

    monkeypatch.setattr("repowatch.repository_analysis.run_analysis", synthetic_run)
    asyncio.run(engine.analyze({}, lambda *_: None))


def test_output_protocol_status_does_not_migrate_credentials(service):  # noqa: F811
    execution, _, _ = service
    settings = AnalysisModel(execution.store, execution.github)
    settings.save(
        version=0, enabled=True, model="glm-4.7", api="chat_completions",
        base_url="https://open.bigmodel.cn/api/paas/v4", api_key="private-test-model-key",
    )
    with execution.store.connection() as db:
        before = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    assert settings.status()["output_protocol"] == "json_object"
    assert settings.status()["finalization_mode"] == "glm47_direct"
    with execution.store.connection() as db:
        after = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    assert before == after
    assert "private-test-model-key" not in json.dumps(settings.status())


@pytest.mark.parametrize("effort,fast", [("auto", True), ("default", False)])
def test_saved_provider_wires_direct_report_mode_without_rewriting_credentials(
    service, monkeypatch, effort, fast,  # noqa: F811
):
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(
        version=0, enabled=True, model="glm-4.7", api="chat_completions",
        base_url="https://open.bigmodel.cn/api/paas/v4", api_key="synthetic-key",
        reasoning_effort=effort,
    )
    with execution.store.connection() as db:
        before = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())

    async def synthetic_run(model, context, event, *, reasoning_effort):
        assert isinstance(model.delegate, JsonObjectChatModel)
        assert model.delegate.fast_finalization is fast
        assert reasoning_effort is None
        return {"summary": "Synthetic only", "limitations": [], "findings": []}

    monkeypatch.setattr("repowatch.repository_analysis.run_analysis", synthetic_run)
    result = asyncio.run(engine.analyze({}, lambda *_: None))
    assert result["model"] == "glm-4.7"
    with execution.store.connection() as db:
        after = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    assert before == after


def test_timeout_retries_are_bounded_and_reuse_the_same_call(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.RETRY_DELAY_SECONDS", 0)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    model = ReviewModel()
    attempts = {"count": 0}
    requests = []
    original = model.get_response

    async def delayed(*args, **kwargs):
        attempts["count"] += 1
        requests.append(kwargs)
        if attempts["count"] in (1, 5):
            raise APITimeoutError(request=httpx.Request("POST", "https://provider.example"))
        return await original(*args, **kwargs)

    model.get_response = delayed
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(report["handoffs"]) == 3
    assert sum(kind == "timeout_retry" for kind, _ in events) == 2
    assert sum(kind == "retry_call" for kind, _ in events) == 2
    assert sum(kind == "model_call" for kind, _ in events) == 6
    assert sum(kind == "source" for kind, _ in events) == 1
    assert attempts["count"] == 8
    assert requests[0] == requests[1] and requests[4] == requests[5]
    assert REQUEST_TIMEOUT_SECONDS == 120 and RETRY_DELAY_SECONDS == 2
    assert REQUEST_TIMEOUT.read == 120 and REQUEST_TIMEOUT.connect == 10
    assert ANALYSIS_TIMEOUT_SECONDS == 600


def test_timeout_retries_stop_after_two_requests(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.RETRY_DELAY_SECONDS", 0)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    model = BrowsingModel()

    async def stalled(*args, **kwargs):
        raise TimeoutError("provider stalled")

    model.get_response = stalled
    with pytest.raises(AnalysisFailure, match="analysis_provider_timeout_exhausted"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert sum(kind == "timeout_retry" for kind, _ in events) == 1
    assert sum(kind == "retry_call" for kind, _ in events) == 1


def test_timeout_retry_budget_is_shared_across_roles(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.RETRY_DELAY_SECONDS", 0)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model = ReviewModel()
    original = model.get_response
    attempts, events = [], []

    async def sometimes_stalled(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) in (1, 5, 7):
            raise TimeoutError()
        return await original(*args, **kwargs)

    model.get_response = sometimes_stalled
    with pytest.raises(AnalysisFailure, match="analysis_provider_timeout_exhausted"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(attempts) == 7
    assert sum(kind == "timeout_retry" for kind, _ in events) == 2


def test_cancel_during_backoff_does_not_retry():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events = ReviewModel(), []

    async def stalled(*args, **kwargs):
        raise TimeoutError()

    model.get_response = stalled

    async def cancel():
        retry_started = asyncio.Event()

        def record(kind, role):
            events.append((kind, role))
            if kind == "timeout_retry":
                retry_started.set()

        task = asyncio.create_task(run_analysis(model, context, record))
        await asyncio.wait_for(retry_started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert not any(kind == "retry_call" for kind, _ in events)


def test_call_deadline_cancels_hanging_requests(monkeypatch):
    monkeypatch.setattr("repowatch.repository_analysis.RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("repowatch.repository_analysis.REQUEST_TIMEOUT_SECONDS", 0.01)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, cancelled = ReviewModel(), []

    async def hanging(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    model.get_response = hanging
    with pytest.raises(AnalysisFailure, match="analysis_provider_timeout_exhausted"):
        asyncio.run(run_analysis(model, context))
    assert len(cancelled) == 2


def test_non_timeout_errors_are_not_retried():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    model, events = ReviewModel(), []

    async def rejected(*args, **kwargs):
        raise ModelBehaviorError("invalid response")

    model.get_response = rejected
    with pytest.raises(ModelBehaviorError):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert not any(kind == "timeout_retry" for kind, _ in events)


@pytest.mark.parametrize("burst,oversized", [(1, False), (20, False), (1, True)])
def test_continuous_browsing_reserves_all_stages_and_forces_native_handoffs(burst, oversized):
    context = {"files": {"app.py": "print('ok')", "large.py": "x" * 16000},
               "config": {}, "jobs": [], "test_result": None}
    events = []
    model = BrowsingModel(burst=burst, oversized=oversized)
    report = asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert len(report["handoffs"]) == 3
    assert [agent for kind, agent in events if kind == "agent"] == list(ROLES)
    assert [agent for kind, agent in events if kind == "budget"] == list(ROLES)
    assert model.calls <= MAX_TURNS
    role, reads = None, {role: 0 for role in ROLES}
    for kind, name in events:
        if kind == "agent":
            role = name
        elif kind == "source":
            reads[role] += 1
    assert all(reads[role] <= STAGE_LIMITS[role][1] for role in ROLES)
    for role in ROLES:
        assert sum(item == ("model_call", role) for item in events) <= STAGE_LIMITS[role][0] + 1


def test_provider_ignoring_forced_handoff_is_rejected_without_fabricated_handoff():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    model = BrowsingModel(ignore_choice=True)
    with pytest.raises(AnalysisFailure, match="analysis_handoff_not_followed"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert not any(kind == "handoff" for kind, _ in events)
    assert model.calls == STAGE_LIMITS[ROLES[0]][0] + 1


def test_reviewer_cannot_continue_reading_after_finalization_budget():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    model = BrowsingModel()
    model.ignore_final = True
    with pytest.raises(AnalysisFailure, match="analysis_finalization_not_followed"):
        asyncio.run(run_analysis(model, context, lambda *item: events.append(item)))
    assert sum(kind == "handoff" for kind, _ in events) == 3
    assert model.calls == MAX_TURNS


def prepare_tested(execution_fixture):
    execution, _, _ = execution_fixture
    env = prepare(execution_fixture)
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    return execution.get(env["id"])


def test_native_source_tools_handoffs_and_evidence_validation():
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    events = []
    report = asyncio.run(run_analysis(ReviewModel(), context, lambda *args: events.append(args)))
    assert report["files_read"] == ["app.py"]
    assert report["execution_read"]
    assert len(report["handoffs"]) == 3
    assert ("source", "app.py") in events
    assert report["findings"][0]["evidence"][0]["quote"] == "print('ok')"
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        asyncio.run(run_analysis(ReviewModel(invented=True), context))
    with pytest.raises(AnalysisFailure, match="analysis_handoff_not_followed"):
        asyncio.run(run_analysis(ReviewModel(skip_review=True), context))
    empty = asyncio.run(run_analysis(ReviewModel(findings=False), context))
    assert empty["findings"] == []


def test_snapshot_bounds_excludes_credentials_and_never_executes_code(tmp_path):
    (tmp_path / ".env").write_text("SECRET_VALUE=do-not-read")
    (tmp_path / "credentials.json").write_text('{"secret":"do-not-read"}')
    (tmp_path / "app.py").write_text("raise RuntimeError('must not execute')\n" + "x" * 20000)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("hidden")
    snapshot = source_snapshot(tmp_path)
    assert list(snapshot["files"]) == ["app.py"]
    assert len(snapshot["files"]["app.py"]) == 16000
    assert snapshot["coverage"]["truncated"] == ["app.py"]
    assert "do-not-read" not in json.dumps(snapshot)


def test_findings_cannot_quote_unread_lines():
    context = {"files": {"app.py": "print('ok')\nsecret_behavior()"},
               "config": {}, "jobs": [], "test_result": None}
    report = asyncio.run(run_analysis(ReviewModel(), context))
    report["read_ranges"] = {"app.py": [[1, 1]]}
    evidence = report["findings"][0]["evidence"][0]
    evidence.update(start_line=2, end_line=2, quote="secret_behavior()")
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        validate_report(report, context)


def test_new_execution_supersedes_waiting_analysis_and_invalidates_old_report(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    engine = Engine()
    engine.enabled = False
    analyses = Analyses(execution, engine)
    asyncio.run(analyses.tick())
    old = analyses.list(env["id"])[0]
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    asyncio.run(analyses.tick())
    newer = analyses.list(env["id"])[0]
    assert newer["id"] != old["id"]
    assert analyses.get(old["id"])["status"] == "superseded"
    assert not analyses.get(old["id"])["current"]
    engine.enabled = True
    asyncio.run(analyses.tick())
    assert analyses.get(newer["id"])["status"] == "completed"
    env = execution.get(env["id"])
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    assert not analyses.get(newer["id"])["current"]


def test_failed_tests_are_eligible_and_do_not_confirm_findings(service):  # noqa: F811
    execution, _, runtime = service
    env = prepare(service)
    runtime.code = 1
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    result = analyses.list(env["id"])[0]
    assert result["status"] == "completed"
    assert result["findings"][0]["status"] == "waiting_reproduction"
    with execution.store.connection() as db:
        context = json.loads(db.execute(
            "SELECT context FROM repository_analyses WHERE id=?", (result["id"],),
        ).fetchone()["context"])
    assert context["test_result"]["exit_code"] == 1


def test_queue_does_not_analyze_active_execution(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    execution.action(env["id"], env["version"], "test")
    analyses = Analyses(execution, Engine())
    with pytest.raises(Conflict, match="environment_busy"):
        analyses.enqueue(env["id"])
    assert asyncio.run(analyses.tick()) is False


def test_automatic_queue_waits_for_model_then_resumes_once(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    engine = Engine()
    engine.enabled = False
    analyses = Analyses(execution, engine)
    assert asyncio.run(analyses.tick()) is False
    row = analyses.list(env["id"])[0]
    assert row["status"] == "waiting_model" and row["report"] is None
    assert analyses.enqueue(env["id"]) == row["id"]
    engine.enabled = True
    assert asyncio.run(analyses.tick())
    result = analyses.get(row["id"])
    assert result["status"] == "completed"
    assert result["findings"][0]["status"] == "waiting_reproduction"
    assert len([e for e in result["events"] if e["kind"] == "handoff"]) == 3
    assert asyncio.run(analyses.tick()) is False
    assert len(analyses.list(env["id"])) == 1
    assert execution.store.dashboard()["findings"][0]["id"] == result["findings"][0]["id"]


def test_real_feedback_requires_personal_confirmation_and_never_repairs(service):  # noqa: F811
    execution, _, runtime = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    finding = row["findings"][0]
    with pytest.raises(Conflict, match="personal_reproduction"):
        analyses.feedback(row["id"], finding["id"], 1, "confirmed", "", False)
    with pytest.raises(Conflict, match="personal_reproduction"):
        analyses.feedback(row["id"], finding["id"], 1, "confirmed", "Observed fixture", False)
    result = analyses.feedback(row["id"], finding["id"], 1, "confirmed", "", True)
    assert result["findings"][0]["status"] == "confirmed"
    assert result["findings"][0]["feedback"]["note"] == ""
    assert result["findings"][0]["feedback"]["revision"] == env["revision"]
    with pytest.raises(Conflict, match="finding_changed"):
        analyses.feedback(row["id"], finding["id"], 1, "confirmed", "Again", True)
    assert runtime.calls == ["setup", "test"]


@pytest.mark.parametrize("decision", ["confirmed", "not_reproduced"])
@pytest.mark.parametrize("note", ["", "   ", "  Observed fixture  "])
def test_feedback_notes_are_optional_and_preserved(service, decision, note):  # noqa: F811
    execution, _, runtime = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    finding = row["findings"][0]
    result = analyses.feedback(
        row["id"], finding["id"], finding["version"], decision, note,
        personally_reproduced=decision == "confirmed",
    )
    saved = result["findings"][0]
    assert saved["status"] == decision
    assert saved["feedback"]["note"] == note.strip()
    assert saved["feedback"]["actor"] == "owner"
    assert saved["feedback"]["revision"] == env["revision"]
    assert saved["version"] == finding["version"] + 1
    assert runtime.calls == ["setup", "test"]


def test_config_changes_invalidate_feedback_and_do_not_invent_new_evidence(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    execution.configure(env["id"], env["version"], {**env["config"], "test": "echo changed"})
    assert not analyses.get(row["id"])["current"]
    with pytest.raises(Conflict, match="analysis_stale"):
        analyses.feedback(row["id"], row["findings"][0]["id"], 1, "confirmed", "Done", True)
    with pytest.raises(Conflict, match="analysis_execution_required"):
        analyses.enqueue(env["id"])


def test_interrupted_review_is_not_automatically_replayed(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    analysis_id = analyses.enqueue(env["id"])
    with execution.store.connection() as db:
        db.execute("UPDATE repository_analyses SET status='running',lease=?", (time.time() - 1,))
    assert asyncio.run(analyses.tick()) is False
    row = analyses.get(analysis_id)
    assert row["status"] == "interrupted" and row["report"] is None
    analyses.retry(analysis_id, row["version"])
    assert asyncio.run(analyses.tick())
    assert analyses.get(analysis_id)["status"] == "completed"


def test_model_failure_does_not_persist_provider_exception_or_claim_findings(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)

    class Unavailable(Engine):
        async def analyze(self, context, event):
            raise RuntimeError("provider credential must never appear")

    analyses = Analyses(execution, Unavailable())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    assert row["status"] == "failed" and row["findings"] == []
    assert "provider credential" not in json.dumps(row)


@pytest.mark.parametrize("exception,code", [
    (MaxTurnsExceeded("private-provider-payload"), "analysis_max_turns_exceeded"),
    (ModelBehaviorError("private-provider-payload"), "analysis_model_output_invalid"),
    (TimeoutError("private-provider-payload"), "analysis_deadline_exceeded"),
    (AnalysisFailure("analysis_handoff_not_followed"), "analysis_handoff_not_followed"),
    (BadRequestError(
        "private-provider-payload",
        response=httpx.Response(400, request=httpx.Request("POST", "https://provider.example")),
        body={"api_key": "private-provider-payload"},
    ), "analysis_provider_request_rejected"),
])
def test_failure_stage_and_retry_attempt_are_persisted_without_secrets(
    service, exception, code, caplog,  # noqa: F811
):
    execution, _, _ = service
    env = prepare_tested(service)

    class Failing(Engine):
        async def analyze(self, context, event):
            event("agent", ROLES[1])
            event("model_call", ROLES[1])
            raise exception

    analyses = Analyses(execution, Failing())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    assert row["error"] == code and row["findings"] == []
    assert row["failure_details"]["stage"] == ROLES[1]
    assert row["failure_details"]["exception_type"] == type(exception).__name__
    assert row["failure_details"]["http_status"] == getattr(exception, "status_code", None)
    assert row["progress"]["model_calls"] == 1
    assert "private-provider-payload" not in json.dumps(row) + caplog.text
    queued = analyses.retry(row["id"], row["version"])
    assert queued["failure_details"] is None and queued["progress"]["model_calls"] == 0
    analyses.engine = Engine()
    asyncio.run(analyses.tick())
    completed = analyses.get(row["id"])
    assert completed["status"] == "completed"
    assert completed["failure_details"] is None
    assert completed["progress"]["model_calls"] == 6
    assert sum(event["kind"] == "attempt" for event in completed["events"]) == 2


def test_analysis_memory_is_repository_and_revision_scoped(service):  # noqa: F811
    from repowatch.memory import Memory
    execution, repo, _ = service
    env = prepare_tested(service)
    memory = Memory(execution.store)
    memory.create(repo["id"], category="testing", title="Matching", content="scoped reference",
                  revision=env["revision"])
    memory.create(repo["id"], category="testing", title="Old", content="wrong revision",
                  revision="b" * 40)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    references = analyses.list(env["id"])[0]["memory_references"]
    assert [ref["title"] for ref in references] == ["Matching"]


def test_analysis_memory_ranking_prefers_relevant_issue_text(service):  # noqa: F811
    from repowatch.memory import Memory
    execution, repo, _ = service
    env = prepare_tested(service)
    memory = Memory(execution.store)
    memory.create(repo["id"], category="testing", title="Unrelated note",
                  content="Other setup detail", revision=env["revision"])
    memory.create(repo["id"], category="testing", title="Fixture suspected behavior",
                  content="Scoped reference for this issue", revision=env["revision"])
    analyses = Analyses(execution, Engine())
    analyses.enqueue(
        env["id"], env["version"], manual=True,
        issue={"id": "issue_test", "number": 1, "title": "Fixture suspected behavior",
               "body": "app.py", "state": "open", "author": "alice", "url": None,
               "updated_at": None},
    )
    asyncio.run(analyses.tick())
    references = analyses.list(env["id"])[0]["memory_references"]
    assert references[0]["title"] == "Fixture suspected behavior"
    assert "Unrelated note" in [item["title"] for item in references]


def test_provider_key_encryption_validation_and_rotation(service):  # noqa: F811
    execution, _, _ = service
    settings = AnalysisModel(execution.store, execution.github)
    assert settings.status()["version"] == 0
    args = dict(version=0, enabled=True, model="test-model", api="responses",
                base_url="https://provider.example/v1", api_key="private-test-model-key")
    saved = settings.save(**args)
    assert saved["has_key"] and "private-test-model-key" not in json.dumps(saved)
    with execution.store.connection() as db:
        raw = db.execute("SELECT * FROM analysis_model").fetchone()
        assert "private-test-model-key" not in str(dict(raw))
    with pytest.raises(Conflict, match="analysis_model_changed"):
        settings.save(**args)
    with pytest.raises(Conflict, match="new_key_required"):
        settings.save(**{**args, "version": 1, "base_url": "https://other.example", "api_key": ""})
    saved = settings.save(**{**args, "version": 1, "api_key": "", "enabled": False})
    assert saved["version"] == 2 and not saved["enabled"]
    for url in ("http://external.example", "http://8.8.8.8/v1",
                "https://user:pass@example.com",
                "https://example.com?secret=1", "http://[", "https://example.com:bad"):
        with pytest.raises(Conflict, match="analysis_model_invalid"):
            settings.save(**{**args, "version": 2, "base_url": url})


def test_provider_settings_accept_rfc1918_http_base_url(service):  # noqa: F811
    execution, _, _ = service
    settings = AnalysisModel(execution.store, execution.github)
    saved = settings.save(
        version=0, enabled=False, model="local-model", api="chat_completions",
        base_url="http://172.16.116.14:3000/v1", api_key="synthetic-local-key",
    )
    assert saved["base_url"] == "http://172.16.116.14:3000/v1"


def test_reasoning_setting_is_saved_without_changing_keys_and_rejects_invalid_values(
    service,  # noqa: F811
):
    execution, _, _ = service
    settings = AnalysisModel(execution.store, execution.github)
    args = dict(version=0, enabled=True, model="glm-5.3", api="chat_completions",
                base_url="https://provider.example/v1", api_key="private-model-key")
    saved = settings.save(**args)
    assert saved["reasoning_effort"] == "auto"
    assert saved["effective_reasoning_effort"] == "low"
    with execution.store.connection() as db:
        encrypted = db.execute("SELECT encrypted_key FROM analysis_model").fetchone()[0]
    saved = settings.save(**{**args, "version": 1, "api_key": "", "reasoning_effort": "default"})
    assert saved["effective_reasoning_effort"] is None
    with execution.store.connection() as db:
        assert db.execute("SELECT encrypted_key FROM analysis_model").fetchone()[0] == encrypted
    for effort in ("invalid", "medium"):
        with pytest.raises(Conflict, match="analysis_model_invalid"):
            settings.save(**{**args, "version": 2, "reasoning_effort": effort})


def test_analysis_apis_require_owner_csrf_and_reject_forged_feedback(tmp_path):
    app = create_app(
        Path(tmp_path) / "api.db", start_worker=False, github_client=Remote().client,
        credential_vault=MemoryVault(), execution_runtime=Runtime(),
        repository_source=Source(), analysis_engine=Engine(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/analysis-model").status_code == 401
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        assert client.post("/api/analysis-model", json={}).status_code == 403
        invalid = client.post("/api/analysis-model", headers=headers,
                              json={"api_key": "private-value", "unexpected": True})
        assert invalid.status_code == 422 and "private-value" not in invalid.text
        execution = app.state.execution
        execution.github.connect("alice", TOKEN_A)
        repo = execution.store.add_repository("alice/project", "https://github.com/alice/project")
        env = prepare_tested((execution, repo, execution.runtime))
        url = f"/api/environments/{env['id']}/analyses"
        response = client.post(url, headers=headers, json={"version": env["version"]})
        assert response.status_code == 202
        asyncio.run(app.state.analyses.tick())
        row = client.get(url).json()[0]
        assert "files" not in row and row["report"]["files_read"] == ["app.py"]
        feedback_url = f"/api/analyses/{row['id']}/findings/{row['findings'][0]['id']}/feedback"
        assert client.post(feedback_url, headers=headers, json={
            "version": 1, "decision": "confirmed", "personally_reproduced": True,
            "note": "Observed", "source_kind": "existing_issue",
        }).status_code == 422
        assert client.post(feedback_url, json={
            "version": 1, "decision": "confirmed", "personally_reproduced": True,
        }).status_code == 403
        assert client.post(feedback_url, headers=headers, json={
            "version": 1, "decision": "confirmed",
        }).status_code == 409
        confirmed = client.post(feedback_url, headers=headers, json={
            "version": 1, "decision": "confirmed", "personally_reproduced": True,
        })
        assert confirmed.status_code == 200
        finding = confirmed.json()["findings"][0]
        assert finding["status"] == "confirmed"
        assert finding["feedback"]["note"] == ""
