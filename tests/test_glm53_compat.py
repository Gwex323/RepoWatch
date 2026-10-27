"""Ark Coding GLM report contracts using the real SDK and synthetic HTTP responses."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from agents import AgentOutputSchema, ModelSettings
from agents.models.interface import ModelTracing
from openai import AsyncOpenAI
from test_execution import service  # noqa: F401

from repowatch.analysis_model import (
    AnalysisModel,
    JsonObjectChatModel,
    finalization_mode,
    output_protocol,
    separate_review_schema_from_tools,
)
from repowatch.diagnosis import Advice
from repowatch.repository_analysis import ROLES, STAGE_LIMITS, AnalysisFailure

ARK = "https://ark.cn-beijing.volces.com/api/coding/v3"


@pytest.mark.parametrize("base_url,model,api,compatible", [
    (ARK, "glm-5.3", "chat_completions", True),
    (ARK + "/", "GLM-5.3-FLASH", "chat_completions", True),
    (ARK.replace(".com/", ".com:443/"), "glm-5.3", "chat_completions", True),
    (ARK, "glm-5.3", "responses", False),
    (ARK, "other-model", "chat_completions", False),
    (ARK.replace("https:", "http:"), "glm-5.3", "chat_completions", False),
    (ARK.replace(".com/", ".com:8443/"), "glm-5.3", "chat_completions", False),
    (ARK.replace(".com/", ".com.example/"), "glm-5.3", "chat_completions", False),
    (ARK.replace("/coding", ""), "glm-5.3", "chat_completions", False),
    ("https://gateway.example/v1", "glm-5.3", "chat_completions", False),
])
def test_json_mode_is_limited_to_ark_coding_glm53(base_url, model, api, compatible):
    config = {"base_url": base_url, "model": model, "api": api}
    assert output_protocol(config) == ("json_object" if compatible else "json_schema")
    assert separate_review_schema_from_tools(config) is compatible
    # GLM-4.7's auto finalization disables thinking; GLM-5.3 must retain its settings.
    assert finalization_mode(config) == "provider_default"


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
@pytest.mark.parametrize("effort,expected_effort", [("auto", "low"), ("default", None),
                                                 ("high", "high")])
@pytest.mark.parametrize("closing,report_case", [
    (True, "valid"), (True, "recover_json"), (False, "recover_json"),
    (False, "invalid_schema"), (True, "invalid_json"),
])
def test_saved_glm_config_preserves_reads_handoffs_and_report_validation(
    service, monkeypatch, model, effort, expected_effort, closing, report_case,  # noqa: F811
):
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(version=0, enabled=True, model=model, api="chat_completions",
                base_url=ARK, api_key="synthetic-not-a-real-key", reasoning_effort=effort)
    with execution.store.connection() as db:
        before = dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())
    assert engine.status()["output_protocol"] == "json_object"
    assert engine.status()["finalization_mode"] == "provider_default"
    for index, role in enumerate(ROLES):
        _, reads, chars = STAGE_LIMITS[role]
        turns = 2 if index == 0 else int(index == 3 and not closing)
        monkeypatch.setitem(STAGE_LIMITS, role, (turns, reads, chars))

    requests, events = [], []
    expected_tools = ["read_execution_records", "read_source_file", *[
        f"transfer_to_{role}" for role in ROLES[1:]
    ]]

    async def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/api/coding/v3/chat/completions"
        assert payload.get("reasoning_effort") == expected_effort
        assert "thinking" not in payload
        turn = len(requests)
        if turn <= len(expected_tools):
            name = expected_tools[turn - 1]
            assert "response_format" not in payload
            assert payload["tool_choice"] == {"type": "function", "function": {"name": name}}
            assert [tool["function"]["name"] for tool in payload["tools"]] == [name]
            args = {"path": "app.py", "start_line": 1, "end_line": 1} if turn == 2 else {}
            message = {"content": None, "tool_calls": [{
                "id": f"call_{turn}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]}
            reason = "tool_calls"
        else:
            if payload.get("tools"):
                assert "response_format" not in payload
            else:
                assert payload["tool_choice"] == "none"
                assert payload["response_format"] == {"type": "json_object"}
            report = {"summary": "Synthetic review", "limitations": [], "findings": []}
            content = json.dumps(report)
            if report_case == "invalid_schema":
                content = '{"summary":"Missing required fields"}'
            elif report_case == "invalid_json" or report_case == "recover_json" and turn == 6:
                content = "{"
            if turn == 7:
                assert [m["role"] for m in payload["messages"]] == ["system", "user"]
                packet = json.loads(payload["messages"][1]["content"])
                assert packet["execution_was_read"] is True
                assert packet["source_evidence"][0]["source_blocks"] == [
                    {"start_line": 1, "end_line": 1, "text": "print('ok')"},
                ]
            message, reason = {"content": content}, "stop"
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": reason,
                         "message": {"role": "assistant", **message}}],
        })

    @asynccontextmanager
    async def client(**kwargs):
        async with kwargs.pop("http_client") as original:
            async with AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(transport), event_hooks=original.event_hooks,
            )) as model_client:
                yield model_client

    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", client)
    context = {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}
    if report_case in {"invalid_schema", "invalid_json"}:
        code = ("analysis_report_invalid" if report_case == "invalid_schema"
                else "analysis_json_invalid")
        with pytest.raises(AnalysisFailure, match=code):
            asyncio.run(engine.analyze(context, lambda *item: events.append(item)))
    else:
        report = asyncio.run(engine.analyze(context, lambda *item: events.append(item)))
        assert len(report["handoffs"]) == 3
        assert report["execution_read"] and report["files_read"] == ["app.py"]
        assert report["reasoning_effort"] == expected_effort
    assert len(requests) == (6 if report_case == "valid" else 7)
    assert sum(kind == "format_retry" for kind, _ in events) == int(report_case != "valid")
    assert not any(kind == "finalization_thinking" for kind, _ in events)
    with execution.store.connection() as db:
        assert before == dict(db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone())


@pytest.mark.parametrize("positional", [True, False])
def test_json_adapter_preserves_schema_in_prompt_for_shared_callers(positional):
    schema = AgentOutputSchema(Advice)

    async def transport(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        prompt = payload["messages"][0]["content"]
        assert prompt.startswith("Synthetic instructions")
        assert json.loads(prompt.split("matching this schema:\n", 1)[1]) == schema.json_schema()
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": "glm-5.3",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "summary": "Synthetic", "evidence": [], "steps": [],
                    "confidence": "uncertain", "patch": {"install": None, "test": None,
                    "start": None, "port": None, "install_network": None},
                }),
            }}],
        })

    async def run():
        async with AsyncOpenAI(api_key="synthetic-key", base_url=ARK,
                               http_client=httpx.AsyncClient(
                                   transport=httpx.MockTransport(transport),
                               )) as client:
            kwargs = dict(model_settings=ModelSettings(), tools=[], handoffs=[],
                          output_schema=schema, tracing=ModelTracing.DISABLED)
            model = JsonObjectChatModel("glm-5.3", client)
            if positional:
                result = await model.get_response("Synthetic instructions", "Fixture", **kwargs)
            else:
                result = await model.get_response(system_instructions="Synthetic instructions",
                                                  input="Fixture", **kwargs)
            assert schema.validate_json(result.output[0].content[0].text).summary == "Synthetic"

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["diagnose", "repair"])
def test_shared_glm_operations_select_json_adapter(service, monkeypatch, operation):  # noqa: F811
    execution, _, _ = service
    engine = AnalysisModel(execution.store, execution.github)
    engine.save(version=0, enabled=True, model="glm-5.3", api="chat_completions",
                base_url=ARK, api_key="synthetic-key", reasoning_effort="high")

    async def runner(model, context, *args, reasoning_effort=None, **kwargs):
        assert type(model.delegate) is JsonObjectChatModel
        assert model.delegate.fast_finalization is False
        assert reasoning_effort == "high"
        return {"summary": "Synthetic fixture"}

    target = ("repowatch.diagnosis.run_agents" if operation == "diagnose"
              else "repowatch.repair_model.run_repair")
    monkeypatch.setattr(target, runner)
    result = asyncio.run(engine.diagnose({}) if operation == "diagnose"
                         else engine.repair({}, lambda *_: None, workspace=None))
    assert result["reasoning_effort"] == "high"
