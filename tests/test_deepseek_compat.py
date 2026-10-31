"""DeepSeek wire-contract regressions; no real credentials or model requests."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from agents import Agent, AgentOutputSchema, ModelSettings, function_tool, handoff
from agents.models.interface import ModelTracing
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from test_execution import service  # noqa: F401
from test_repository_analysis import force_fixture_closing

from repowatch.analysis_model import (
    AnalysisModel,
    DeepSeekChatModel,
    deepseek_chat_compatibility,
    effective_reasoning,
    finalization_mode,
    output_protocol,
)
from repowatch.repository_analysis import ROLES, AnalysisFailure, Review


@pytest.mark.parametrize("base_url,api,compatible", [
    ("https://api.deepseek.com", "chat_completions", True),
    ("https://api.deepseek.com/v1/", "chat_completions", True),
    ("https://api.deepseek.com:443/beta", "chat_completions", True),
    ("https://api.deepseek.com", "responses", False),
    ("https://gateway.example/v1", "chat_completions", False),
    ("https://api.deepseek.com.example/v1", "chat_completions", False),
    ("https://api.deepseek.com/custom", "chat_completions", False),
    ("https://api.deepseek.com:8443", "chat_completions", False),
])
def test_deepseek_compatibility_is_limited_to_official_chat_contract(base_url, api, compatible):
    config = {"base_url": base_url, "api": api, "model": "deepseek-flash",
              "reasoning_effort": "high"}
    assert deepseek_chat_compatibility(config) is compatible
    assert output_protocol(config) == ("json_object" if compatible else "json_schema")
    assert finalization_mode(config) == ("deepseek_direct" if compatible else "provider_default")
    assert effective_reasoning(config) == (None if compatible else "high")


def completion(message, reason="stop"):
    return httpx.Response(200, json={
        "id": "synthetic", "object": "chat.completion", "created": 1,
        "model": "deepseek-flash", "choices": [{
            "index": 0, "finish_reason": reason, "message": {"role": "assistant", **message},
        }],
    })


def mock_provider(monkeypatch, transport):
    @asynccontextmanager
    async def client(**kwargs):
        async with kwargs.pop("http_client") as original:
            async with AsyncOpenAI(
                **kwargs, http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(transport), event_hooks=original.event_hooks,
                ),
            ) as model_client:
                yield model_client

    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", client)


@pytest.mark.parametrize("effort", ["auto", "default", "low", "medium", "high", "max"])
@pytest.mark.parametrize("invalid_report", [False, True])
@pytest.mark.parametrize("handoff_behavior", ["valid", "recover", "invalid"])
def test_saved_deepseek_config_runs_forced_reads_handoffs_and_validated_report(
    service, monkeypatch, effort, invalid_report, handoff_behavior,  # noqa: F811
):
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(
        version=0, enabled=True, model="deepseek-flash", api="chat_completions",
        base_url="https://api.deepseek.com", api_key="synthetic-not-a-real-key",
        reasoning_effort=effort,
    )
    with execution.store.connection() as db:
        before = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    status = engine.status()
    assert status["reasoning_effort"] == effort
    assert status["effective_reasoning_effort"] is None
    assert status["output_protocol"] == "json_object"
    assert status["finalization_mode"] == "deepseek_direct"
    assert "synthetic-not-a-real-key" not in json.dumps(status)
    # Close stages as soon as mandatory reads finish, exercising named handoffs
    # rather than relying only on voluntary tool_choice=auto transfers.
    force_fixture_closing(monkeypatch)
    requests, events = [], []
    next_tool = 0
    mismatch_emitted = False
    expected_tools = ["read_execution_records", "read_source_file", *[
        f"transfer_to_{role}" for role in ROLES[1:]
    ]]

    def record(kind, value):
        events.append((kind, value))
        if kind == "format_retry" and sum(k == kind for k, _ in events) >= 3:
            raise AnalysisFailure("analysis_report_invalid")  # Caller stops the fixture.
        if kind == "handoff_validation" and not json.loads(value).get("accepted"):
            rejected = [v for k, v in events if k == kind and not json.loads(v).get("accepted")]
            if len(rejected) >= 3:
                raise AnalysisFailure("analysis_tool_arguments_exhausted")

    async def transport(request):
        nonlocal next_tool, mismatch_emitted
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/chat/completions"
        assert payload["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in payload
        assert all(not tool["function"]["strict"] for tool in payload.get("tools", []))
        if next_tool < len(expected_tools):
            name = expected_tools[next_tool]
            assert payload["tool_choice"] == {"type": "function", "function": {"name": name}}
            assert [tool["function"]["name"] for tool in payload["tools"]] == [name]
            args = (
                {"path": "app.py", "start_line": 1, "end_line": 1}
                if name == "read_source_file" else {}
            )
            response_name = name
            if (handoff_behavior == "invalid" and name.startswith("transfer_")
                    or handoff_behavior == "recover" and name.startswith("transfer_")
                    and not mismatch_emitted):
                mismatch_emitted = True
                response_name = "unexpected_handoff"
            else:
                next_tool += 1
            return completion({"content": None, "tool_calls": [{
                "id": f"call_{len(requests)}", "type": "function",
                "function": {"name": response_name, "arguments": json.dumps(args)},
            }]}, "tool_calls")
        assert next_tool == len(expected_tools)
        assert not payload.get("tools")
        assert payload["tool_choice"] == "none"
        assert payload["response_format"] == {"type": "json_object"}
        report = {"summary": "Synthetic review", "limitations": [], "findings": []}
        return completion({"content": json.dumps({"summary": "Missing fields"}
                                                  if invalid_report else report)})

    mock_provider(monkeypatch, transport)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    if handoff_behavior == "invalid":
        with pytest.raises(AnalysisFailure, match="analysis_tool_arguments_exhausted"):
            asyncio.run(engine.analyze(context, record))
        assert len(requests) == 5
        assert not any(kind == "handoff" for kind, _ in events)
    elif invalid_report:
        with pytest.raises(AnalysisFailure, match="analysis_report_invalid"):
            asyncio.run(engine.analyze(context, record))
        assert sum(kind == "format_retry" for kind, _ in events) >= 2
        assert len(requests) == 8 + (handoff_behavior == "recover")
    else:
        report = asyncio.run(engine.analyze(context, record))
        assert len(report["handoffs"]) == 3
        assert sum(kind == "evidence" for kind, _ in events) == 1
        assert report["files_read"] == ["app.py"]
        assert report["reasoning_effort"] is None
        assert len(requests) == 6 + (handoff_behavior == "recover")
    assert sum(kind == "handoff_retry" for kind, _ in events) == {
        "valid": 0, "recover": 1, "invalid": 2,
    }[handoff_behavior]
    validations = [json.loads(value) for kind, value in events if kind == "handoff_validation"]
    assert sum(not item["accepted"] for item in validations) == {
        "valid": 0, "recover": 1, "invalid": 3,
    }[handoff_behavior]
    if handoff_behavior == "invalid":
        assert validations[-1]["recoverable"] is False
        assert validations[-1]["corrections_remaining"] == 0
    assert all("arguments" not in item and "content" not in item for item in validations)
    assert ("provider_compatibility", "deepseek_chat") in events
    with execution.store.connection() as db:
        after = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    assert before == after


def test_deepseek_adapter_keeps_original_sdk_tools_handoffs_and_settings():
    @function_tool
    def synthetic_read() -> str:
        """Return synthetic evidence."""
        return "fixture"

    transfer = handoff(Agent(name="reviewer"))
    settings = ModelSettings(
        reasoning=Reasoning(effort="high"), tool_choice="required",
        extra_body={"thinking": {"type": "enabled"}, "synthetic_flag": True},
    )
    before = settings.to_json_dict()

    async def transport(request):
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["synthetic_flag"] is True
        assert "reasoning_effort" not in payload
        assert payload["tool_choice"] == "required"
        assert payload["response_format"] == {"type": "json_object"}
        assert len(payload["tools"]) == 2
        assert all(tool["function"]["strict"] is False for tool in payload["tools"])
        return completion({"content": '{"summary":"Fixture","limitations":[],"findings":[]}'})

    async def run():
        async with AsyncOpenAI(
            api_key="synthetic-not-a-real-key", base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        ) as client:
            return await DeepSeekChatModel("deepseek-flash", client).get_response(
                system_instructions="Return JSON", input="Fixture", model_settings=settings,
                tools=[synthetic_read], handoffs=[transfer],
                output_schema=AgentOutputSchema(Review),
                tracing=ModelTracing.DISABLED,
            )

    asyncio.run(run())
    assert synthetic_read.strict_json_schema is True
    assert transfer.strict_json_schema is True
    assert settings.to_json_dict() == before


@pytest.mark.parametrize("operation", ["diagnose", "repair"])
def test_deepseek_shared_adapter_is_used_by_diagnosis_and_repair(
    service, monkeypatch, operation,  # noqa: F811
):
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(
        version=0, enabled=True, model="deepseek-flash", api="chat_completions",
        base_url="https://api.deepseek.com/v1", api_key="synthetic-not-a-real-key",
        reasoning_effort="high",
    )

    async def runner(model, context, *args, reasoning_effort=None, **kwargs):
        assert isinstance(model.delegate, DeepSeekChatModel)
        assert reasoning_effort is None
        if operation == "repair":
            assert kwargs["workspace"] == "synthetic-workspace"
        return {"summary": "Synthetic fixture"}

    target = "repowatch.diagnosis.run_agents" if operation == "diagnose" else (
        "repowatch.repair_model.run_repair"
    )
    monkeypatch.setattr(target, runner)
    if operation == "diagnose":
        result = asyncio.run(engine.diagnose({}))
    else:
        result = asyncio.run(engine.repair({}, lambda *_: None, workspace="synthetic-workspace"))
    assert result["reasoning_effort"] is None
