"""Exercise the actual SDK wire request without calling a real provider."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, RunConfig, Runner
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from repowatch.analysis_model import ObservedModel, observe_response
from repowatch.repair_model import RepairStageModel


@pytest.mark.parametrize("role", ["repair_agent", "independent_reviewer"])
def test_output_retry_sends_lower_reasoning_to_chat_provider(role):
    requests, events, observation = [], [], {}

    def record(*event):
        events.append(event)

    async def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        exhausted = payload["reasoning_effort"] != "low"
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{
                "index": 0, "finish_reason": "length" if exhausted else "stop",
                "message": {"role": "assistant", "content": None if exhausted else "complete"},
            }], "usage": {"prompt_tokens": 100, "completion_tokens": 12000 if exhausted else 20,
                         "completion_tokens_details": {
                             "reasoning_tokens": 11992 if exhausted else 8,
                         },
                         "total_tokens": 12100 if exhausted else 120},
        })

    async def observe(response):
        await observe_response(response, observation, record)

    async def run():
        async with AsyncOpenAI(
            api_key="fixture-not-a-real-key", base_url="https://fixture.example/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),
                                          event_hooks={"response": [observe]}),
        ) as client:
            model = RepairStageModel(
                ObservedModel(OpenAIChatCompletionsModel("fixture", client), observation),
                role, SimpleNamespace(check=lambda: None, verification=None),
                # This isolates output recovery after the mandatory evidence read.
                record, lambda: {"verification_read": True}, {"timeouts": 0},
            )
            return await Runner.run(Agent(
                name=role, instructions="Return a short response.", model=model,
                model_settings=ModelSettings(max_tokens=12000, reasoning=Reasoning(effort="high")),
            ), "fixture", run_config=RunConfig(tracing_disabled=True))

    result = asyncio.run(run())
    assert result.final_output == "complete"
    assert [r["reasoning_effort"] for r in requests] == ["high", "low"]
    assert [r["max_tokens"] for r in requests] == [12000, 16000]
    assert all(r["model"] == "fixture" for r in requests)
    assert sum(kind == "output_retry" for kind, _ in events) == 1
    assert "fixture-not-a-real-key" not in json.dumps(events)
