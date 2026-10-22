"""Explicit network checks use synthetic HTTP only, never real credentials."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_github_accounts import MemoryVault
from test_model_providers import catalog, create  # noqa: F401

from repowatch.app import create_app
from repowatch.provider_probe import ProviderProbe
from repowatch.store import Conflict


def draft(providers, **changes):
    return {"version": providers.state()["version"], "base_url": "https://first.example/v1",
            "api": "chat_completions", "api_key": "synthetic-probe-key", **changes}


def run(providers, handler, operation="discover", **changes):
    probe = ProviderProbe(providers, transport=httpx.MockTransport(handler))
    return asyncio.run(getattr(probe, operation)(**draft(providers, **changes)))


def test_discovery_before_any_model_is_entered_is_sorted_bounded_and_read_only(catalog):  # noqa: F811
    providers, _ = catalog
    before = providers.state()
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://first.example/v1/models"
        assert request.method == "GET" and not request.content
        assert request.headers["authorization"] == "Bearer synthetic-probe-key"
        return httpx.Response(200, json={"data": [
            {"id": "model-z"}, {"id": "model-a"}, {"id": "model-z"}, {"id": " "},
            {"id": "bad\nname"}, {"id": "synthetic-probe-key"}, {"id": "x" * 201}, None,
        ]})

    result = run(providers, handler, model=" ")
    assert result == {"models": ["model-a", "model-z"], "truncated": False}
    assert providers.state() == before and len(requests) == 1


def test_existing_provider_uses_saved_key_without_writing_or_revealing_it(catalog):  # noqa: F811
    providers, _ = catalog
    saved = create(providers)
    provider_id = saved["providers"][0]["id"]

    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-first-key"
        return httpx.Response(200, json={"data": [{"id": "general-model"}]})

    result = run(providers, handler, provider_id=provider_id, api_key="")
    assert "synthetic-first-key" not in json.dumps(result)
    assert providers.state() == saved


@pytest.mark.parametrize("case,expected", [
    ("new_address", "analysis_model_new_key_required"),
    ("stale", "model_providers_changed"), ("missing", "model_provider_missing"),
    ("no_key", "analysis_model_key_required"), ("invalid_url", "analysis_model_invalid"),
    ("header", "model_probe_key_invalid"),
])
def test_invalid_or_changed_configuration_never_sends_credentials(catalog, case, expected):  # noqa: F811
    providers, _ = catalog
    saved = create(providers)
    values = {"provider_id": saved["providers"][0]["id"], "api_key": ""}
    if case == "new_address":
        values["base_url"] = "https://changed.example/v1"
    elif case == "stale":
        values["version"] = 0
    elif case == "missing":
        values["provider_id"] = "missing"
    elif case == "no_key":
        values["provider_id"] = None
    elif case == "invalid_url":
        values["base_url"] = "https://user:password@example.com"
    elif case == "header":
        values["api_key"] = "bad\nkey"
    calls = []
    with pytest.raises(Conflict, match=expected):
        run(providers, lambda request: calls.append(request), **values)
    assert calls == [] and providers.state() == saved


@pytest.mark.parametrize("status,expected", [
    (401, "authentication"), (403, "forbidden"), (429, "rate_limited"),
    (404, "models_unsupported"), (405, "models_unsupported"), (500, "service_error"),
    (400, "request_rejected"), (422, "request_rejected"), (302, "redirect"),
])
def test_provider_errors_are_redacted_and_never_retried(catalog, status, expected):  # noqa: F811
    providers, _ = catalog
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.example/steal"},
                              json={"error": "synthetic-probe-key and private provider text"})

    with pytest.raises(Conflict, match="model_probe_" + expected) as error:
        run(providers, handler)
    assert len(calls) == 1 and "synthetic-probe-key" not in str(error.value)


@pytest.mark.parametrize("api,base_url,operation,token_field", [
    ("chat_completions", "https://first.example/v1", "chat/completions", "max_tokens"),
    ("chat_completions", "https://api.openai.com/v1", "chat/completions", "max_completion_tokens"),
    ("responses", "https://first.example/v1", "responses", "max_output_tokens"),
])
def test_connection_makes_one_small_request_and_never_returns_generated_text(
    catalog, api, base_url, operation, token_field,  # noqa: F811
):
    providers, _ = catalog
    calls = []
    before = providers.state()

    def handler(request):
        calls.append(request)
        data = json.loads(request.content)
        assert str(request.url) == base_url + "/" + operation and request.method == "POST"
        assert data["model"] == "chosen-model" and data[token_field] == 32
        assert data["stream"] is False and "tools" not in data
        if api == "responses":
            assert data["store"] is False
            response = {"id": "r", "object": "response", "status": "completed", "output": []}
        else:
            response = {"choices": [{"message": {"role": "assistant",
                                                  "content": "synthetic-probe-key"}}]}
        return httpx.Response(200, json=response)

    result = run(providers, handler, "test_connection", model="chosen-model",
                 api=api, base_url=base_url)
    assert result["connected"] and type(result["latency_ms"]) is int
    assert "synthetic-probe-key" not in json.dumps(result) and len(calls) == 1
    assert providers.state() == before


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": "OK"}]},
                                  {"error": "synthetic-probe-key"}])
def test_http_success_without_a_model_response_is_not_connection_success(catalog, body):  # noqa: F811
    providers, _ = catalog
    with pytest.raises(Conflict, match="model_probe_invalid_response"):
        run(providers, lambda _: httpx.Response(200, json=body), "test_connection", model="model")


def test_missing_model_never_sends_inference_request(catalog):  # noqa: F811
    providers, _ = catalog
    calls = []
    with pytest.raises(Conflict, match="model_probe_model_required"):
        run(providers, lambda r: calls.append(r), "test_connection")
    assert calls == []


def test_large_or_paginated_model_lists_are_bounded(catalog):  # noqa: F811
    providers, _ = catalog
    result = run(providers, lambda _: httpx.Response(200, json={"data": [
        {"id": f"model-{i:04d}"} for i in range(1100)
    ]}))
    assert len(result["models"]) == 1000 and result["truncated"]
    with pytest.raises(Conflict, match="model_probe_response_limit"):
        run(providers, lambda _: httpx.Response(200, content=b"x" * 512001))
    with pytest.raises(Conflict, match="model_probe_invalid_response"):
        run(providers, lambda _: httpx.Response(200, text="<html>private error</html>"))
    with pytest.raises(Conflict, match="model_probe_invalid_response"):
        run(providers, lambda _: httpx.Response(200, content=b"[" * 2000 + b"]" * 2000))


def test_transport_errors_and_total_deadline_are_bounded(catalog, monkeypatch):  # noqa: F811
    providers, _ = catalog

    def unavailable(_):
        raise httpx.ConnectError("private provider failure synthetic-probe-key")

    with pytest.raises(Conflict, match="model_probe_unreachable"):
        run(providers, unavailable)
    monkeypatch.setattr("repowatch.provider_probe.MODEL_LIST_TIMEOUT", .01)
    closed = []

    async def hanging(_):
        try:
            await asyncio.sleep(1)
        finally:
            closed.append(True)

    with pytest.raises(Conflict, match="model_probe_timeout"):
        run(providers, hanging)
    assert closed == [True]


def test_probe_apis_require_owner_csrf_and_mask_invalid_secret_input(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "discovered-model"}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                   "content": "OK"}}]})

    app = create_app(tmp_path / "api.db", start_worker=False, credential_vault=MemoryVault(),
                     provider_probe_transport=httpx.MockTransport(handler))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        path = "/api/model-providers/discover"
        body = {"version": 0, "base_url": "https://first.example/v1", "api_key": "private-key"}
        assert client.post(path, json=body).status_code == 401
        session = client.post("/api/setup", json={"password": "qa-only-owner-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        assert client.post(path, json=body).status_code == 403
        invalid = client.post(path, headers=headers, json={"api_key": "private-key"})
        assert invalid.status_code == 422 and "private-key" not in invalid.text
        before = client.get("/api/model-providers").json()
        result = client.post(path, headers=headers, json=body)
        assert result.status_code == 200 and result.json()["models"] == ["discovered-model"]
        result = client.post("/api/model-providers/test-connection", headers=headers,
                             json={**body, "model": "discovered-model"})
        assert result.status_code == 200 and result.json()["connected"]
        assert len(calls) == 2 and client.get("/api/model-providers").json() == before
