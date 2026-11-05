import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from repowatch.adapters.providers import ModelLimits, ProviderAdapter, ProviderRequestError
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import EventReader
from repowatch.application.budgets import BudgetService
from repowatch.application.diagnostics import DiagnosticService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.providers import ProviderService
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.providers import ProviderModel
from repowatch.operations.migrate import migrate
from repowatch.worker.diagnostics import DiagnosticRunner


class CheckResult(BaseModel):
    passed: bool


@pytest.fixture
def service(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    values = {}
    vault = SimpleNamespace(
        create=lambda ref, secret: values.update({ref: secret}),
        read=lambda ref: values[ref],
        delete=lambda ref: values.pop(ref, None),
    )
    yield ProviderService(store, vault)
    store.close()


def save(service, api):
    return service.save(
        "config",
        {
            "name": "gateway",
            "base_url": "https://gateway.example/v1",
            "api": api,
            "api_key": "test-adapter-private-key",
            "models": [],
        },
    )["providers"][0]["revision_id"]


def response(body, api, *, structured=False):
    if api == "responses":
        output = (
            [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "CheckResult",
                    "arguments": '{"passed":true}',
                    "status": "completed",
                }
            ]
            if structured
            else [
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": body["model"],
                "output": output,
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            },
        )
    message = {"role": "assistant", "content": "ok"}
    if structured:
        message.update(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "CheckResult", "arguments": '{"passed":true}'},
                }
            ],
        )
    return httpx.Response(
        200,
        json={
            "id": "chat_1",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if structured else "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("structured", [False, True])
def test_real_sdk_protocol_manual_model_tool_output_and_limits(
    service, api, structured, monkeypatch
):
    revision_id = save(service, api)
    requests = []

    def wire(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/" + (
            "responses" if api == "responses" else "chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer test-adapter-private-key"
        assert "test-adapter-private-key" not in request.content.decode()
        assert body["model"] == "hand-entered-model"
        assert body["max_output_tokens" if api == "responses" else "max_tokens"] == 32
        assert body["store"] is False
        if structured:
            assert body["parallel_tool_calls"] is False
        return response(body, api, structured=structured)

    monkeypatch.setenv("HTTPS_PROXY", "http://invalid-proxy.example")
    adapter = ProviderAdapter(service, transport=httpx.MockTransport(wire))
    with adapter.session(
        revision_id,
        ProviderModel(model="hand-entered-model"),
        ModelLimits(max_output_tokens=32),
    ) as model:
        assert model.max_retries == 0
        selected = (
            model.with_structured_output(CheckResult, method="function_calling")
            if structured
            else model
        )
        result = selected.invoke([HumanMessage(content="controlled input")])
    assert len(requests) == 1
    assert result.passed if structured else result.text == "ok"


def test_unavailable_model_list_does_not_prevent_manual_model_and_replay_is_read_only(service):
    revision_id = save(service, "chat_completions")
    calls = []

    def wire(request):
        calls.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(404, json={"error": "test-adapter-private-key"})
        return response(json.loads(request.content), "chat_completions")

    adapter = ProviderAdapter(service, transport=httpx.MockTransport(wire))
    result = adapter.list_models("list", revision_id)
    assert not result["available"]
    assert adapter.list_models("list", revision_id) == result
    with adapter.session(revision_id, ProviderModel(model="manual")) as model:
        assert model.invoke("ok").content == "ok"
    assert calls == ["/v1/models", "/v1/chat/completions"]
    assert "test-adapter-private-key" not in json.dumps(EventReader(service.store).read())


def test_explicit_model_list_has_bounded_shape_and_no_implicit_model_selection(service):
    revision_id = save(service, "responses")
    adapter = ProviderAdapter(
        service,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "z"}, {"id": "a"}, {"id": "z"}]},
            )
        ),
    )
    assert adapter.list_models("list", revision_id)["models"] == ["a", "z"]
    assert service.list()[0]["configuration"]["models"] == []
    with pytest.raises(PermissionError):
        adapter.list_models("agent", revision_id, actor="agent")


def test_error_is_redacted_one_attempt_and_does_not_follow_redirects(service):
    revision_id = save(service, "responses")
    calls = []

    def wire(request):
        calls.append(request.url.host)
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "test-adapter-private-key",
                    "type": "authentication_error",
                }
            },
        )

    adapter = ProviderAdapter(service, transport=httpx.MockTransport(wire))
    with adapter.session(revision_id, ProviderModel(model="manual")) as model:
        with pytest.raises(ProviderRequestError) as error:
            model.invoke("hello")
    assert error.value.status_code == 401
    assert not error.value.transient
    assert "test-adapter-private-key" not in str(error.value)
    assert calls == ["gateway.example"]
    redirect = ProviderAdapter(
        service,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                307,
                headers={"location": "https://other.example/models"},
            )
        ),
    )
    assert not redirect.list_models("redirect", revision_id)["available"]


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_diagnostic_runner_uses_own_budget_no_repository_and_keeps_global_pause(service, api):
    revision_id = save(service, api)
    diagnostics, queue = DiagnosticService(service.store), Orchestrator(service.store)
    budgets = BudgetService(service.store, service.store.settings.artifacts_dir)
    result = diagnostics.request(
        "test",
        "provider_connection",
        {"provider_revision_id": revision_id, "model": "manual"},
        BudgetAmounts(
            active_ms=5000, model_calls=2, tokens=1000, repair_rounds=1, recovery_rounds=1
        ),
        expected_revision=1,
        now=0,
        duration=2,
    )
    token = diagnostics.claim(result["task_id"], now=0)
    adapter = ProviderAdapter(
        service,
        transport=httpx.MockTransport(
            lambda request: response(json.loads(request.content), api),
        ),
    )
    runner = DiagnosticRunner(diagnostics, adapter, budgets, queue, clock=lambda: 0)
    assert runner.run(token)["passed"]
    assert budgets.view(result["task_id"])["used"]["model_calls"] == 1
    assert budgets.view(result["task_id"])["used"]["tokens"] == 8
    with service.store.read() as records:
        assert records.find("repositories") == []
        assert records.one("global_control", singleton=1)["paused"] == 1
    assert queue.claim(now=0) is None
