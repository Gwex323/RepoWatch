"""Owner provider catalog, encrypted keys and real SDK transport isolation."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from agents import Model, ModelSettings
from agents.models.interface import ModelTracing
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from test_diagnosis import UNKNOWN_LOG, DiagnosticModel
from test_github_accounts import MemoryVault
from test_repairs import RepairModel, RepairRuntime
from test_repository_analysis import ReviewModel

from repowatch.analysis_model import AnalysisModel
from repowatch.app import create_app
from repowatch.diagnosis import run_agents
from repowatch.github_accounts import GitHubAccounts
from repowatch.memory_evaluation import _model_consistent, model_identity
from repowatch.model_providers import (
    AGENT_ROLES,
    ModelProviders,
    model_for_role,
    reasoning_for_role,
)
from repowatch.repair_model import run_repair
from repowatch.repair_workspace import RepairWorkspace
from repowatch.repository_analysis import run_analysis
from repowatch.store import Conflict, Store


@pytest.fixture
def catalog(tmp_path):
    store = Store(tmp_path / "catalog.db")
    credentials = GitHubAccounts(store, vault=MemoryVault())
    return ModelProviders(store, credentials), AnalysisModel(store, credentials)


def provider(name="供应商甲", url="https://first.example/v1", key="synthetic-first-key"):
    return {"name": name, "base_url": url, "api": "chat_completions", "api_key": key,
            "models": [{"model": "general-model", "reasoning_effort": "auto"},
                       {"model": "specialized-model", "reasoning_effort": "high"}]}


def create(providers, **value):
    return providers.save(version=providers.state()["version"], **{**provider(), **value})


def configure(providers, routes, enabled=True):
    return providers.configure(version=providers.state()["version"], enabled=enabled, routes=routes)


def choice(saved, model="general-model", index=0):
    return {"provider_id": saved["providers"][index]["id"], "model": model}


def test_saved_keys_are_encrypted_kept_on_edit_and_not_returned(catalog):
    providers, engine = catalog
    first = create(providers)
    assert first["providers"][0]["has_key"] and not first["enabled"]
    assert "synthetic-first-key" not in json.dumps(first)
    with providers.store.connection() as db:
        raw = dict(db.execute("SELECT * FROM model_providers").fetchone())
    assert "synthetic-first-key" not in str(raw)
    edited = providers.save(version=first["version"], provider_id=raw["id"],
                            **provider(name="自定义名称", key=""))
    with providers.store.connection() as db:
        assert db.execute("SELECT encrypted_key FROM model_providers").fetchone()[0] == raw[
            "encrypted_key"
        ]
    configure(providers, {"default": choice(edited)})
    assert engine.status()["enabled"] and engine.status()["model"] == "general-model"
    assert engine.legacy_status()["version"] == 0
    reopened = ModelProviders(Store(providers.store.path), providers.credentials)
    assert reopened.state() == providers.state()


@pytest.mark.parametrize("changes", [
    {"base_url": "http://remote.example/v1"}, {"base_url": "https://user:secret@example.com"},
    {"base_url": "https://provider.example/v1?key=private"}, {"base_url": "https://["},
    {"models": [{"model": "same"}, {"model": "same"}]}, {"models": []}, {"name": " "},
    {"models": [{"model": "glm-5.3", "reasoning_effort": "medium"}]},
])
def test_invalid_provider_does_not_change_catalog(catalog, changes):
    providers, _ = catalog
    before = providers.state()
    with pytest.raises(Conflict, match="model_provider_invalid"):
        create(providers, **changes)
    assert providers.state() == before


def test_import_is_atomic_and_optimistic_versions_prevent_overwrite(catalog):
    providers, _ = catalog
    duplicate = [provider(), provider()]
    with pytest.raises(Conflict, match="name_exists"):
        providers.import_providers(version=0, providers=duplicate)
    assert providers.state()["providers"] == [] and providers.state()["version"] == 0
    saved = providers.import_providers(version=0, providers=[
        provider(), provider("供应商乙", "http://127.0.0.1:8080/v1", "synthetic-other-key"),
    ])
    assert len(saved["providers"]) == 2
    with pytest.raises(Conflict, match="model_providers_changed"):
        providers.save(version=0, **provider(name="过期编辑"))
    assert providers.state() == saved


def test_key_rotation_endpoint_changes_and_referenced_models_are_protected(catalog):
    providers, _ = catalog
    saved = create(providers)
    chosen = choice(saved, "specialized-model")
    state = configure(providers, {"default": choice(saved), "repair.repair_agent": chosen})
    provider_id = chosen["provider_id"]
    with pytest.raises(Conflict, match="new_key_required"):
        providers.save(version=state["version"], provider_id=provider_id,
                       **provider(url="https://changed.example/v1", key=""))
    with pytest.raises(Conflict, match="in_use"):
        providers.save(version=state["version"], provider_id=provider_id,
                       **{**provider(), "models": [{"model": "general-model"}]})
    with pytest.raises(Conflict, match="in_use"):
        providers.delete(provider_id, state["version"])
    updated = providers.save(version=state["version"], provider_id=provider_id,
                             **provider(url="https://changed.example/v1", key="new-key"))
    assert updated["routes"]["repair.repair_agent"] == chosen
    configure(providers, {}, enabled=False)
    assert providers.delete(provider_id, providers.state()["version"])["providers"] == []


def test_legacy_configuration_survives_upgrade_and_default_switches(catalog):
    providers, engine = catalog
    legacy = engine.save(version=0, enabled=True, model="old-model", api="responses",
                         base_url="https://old.example/v1", api_key="old-secret")
    with providers.store.connection() as db:
        original = dict(db.execute("SELECT * FROM analysis_model").fetchone())
    assert providers.state()["legacy_available"] and providers.state()["enabled"]
    saved = create(providers)
    configure(providers, {"default": choice(saved), "repair.repair_agent": choice(saved)})
    assert engine.status()["model"] == "general-model"
    with providers.store.connection() as db:
        assert dict(db.execute("SELECT * FROM analysis_model").fetchone()) == original
    engine.save(version=legacy["version"], enabled=False, model="old-model", api="responses",
                base_url="https://old.example/v1", api_key="")
    assert engine.status()["model"] == "old-model" and not engine.status()["enabled"]
    assert "default" not in providers.state()["routes"]
    assert "repair.repair_agent" in providers.state()["routes"]


@pytest.mark.parametrize("case", ["role", "provider", "model", "key", "default"])
def test_invalid_assignments_cannot_enable_partial_configuration(catalog, case):
    providers, _ = catalog
    saved = create(providers, api_key="" if case == "key" else "key")
    routes = {"default": choice(saved)}
    if case == "role":
        routes["invented.agent"] = choice(saved)
    elif case == "provider":
        routes["default"]["provider_id"] = "missing"
    elif case == "model":
        routes["default"]["model"] = "missing"
    elif case == "default":
        routes = {"repair.repair_agent": choice(saved)}
    with pytest.raises(Conflict):
        configure(providers, routes)
    assert not providers.state()["enabled"]


@pytest.mark.parametrize("workflow", ["analysis", "repair", "diagnosis"])
def test_real_transport_routes_keys_models_and_freezes_current_run(catalog, monkeypatch, workflow):
    providers, engine = catalog
    saved = create(providers)
    saved = create(providers, name="供应商乙", base_url="https://second.example/v1",
                   api_key="synthetic-second-key")
    roles = [key.split(".")[1] for key in AGENT_ROLES if key.startswith(workflow + ".")]
    chosen = choice(saved, "specialized-model", 1)
    configure(providers, {"default": choice(saved), f"{workflow}.{roles[-1]}": chosen})
    requests, clients, events = [], [], []

    async def transport(request):
        data = json.loads(request.content)
        requests.append((request.url.host, request.headers["authorization"], data))
        assert "synthetic-first-key" not in request.content.decode()
        assert "synthetic-second-key" not in request.content.decode()
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
            "created": 1, "model": data["model"], "choices": [{"index": 0,
            "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    def client(**kwargs):
        original_http = kwargs.pop("http_client")
        clients.append((kwargs["api_key"], original_http))
        # Keep the production response hook, but make every request synthetic.
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(transport),
            event_hooks=original_http.event_hooks, trust_env=False, follow_redirects=False)
        @asynccontextmanager
        async def managed():
            async with original_http, AsyncOpenAI(**kwargs) as opened:
                yield opened
        return managed()

    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", client)

    async def runner(model, context, event, *, reasoning_effort=None, **options):
        # Change the endpoint after snapshot capture: this run still uses the old selection.
        providers.save(version=providers.state()["version"], provider_id=chosen["provider_id"],
                       **provider("供应商乙", "https://changed.example/v1", "rotated-key"))
        for role in roles:
            effort = reasoning_for_role(model, role, reasoning_effort)
            await model_for_role(model, role).get_response(
                system_instructions="Synthetic transport test", input="safe input",
                model_settings=ModelSettings(
                    reasoning=Reasoning(effort=effort) if effort else None,
                ),
                tools=[], output_schema=None, handoffs=[], tracing=ModelTracing.DISABLED,
            )
        return {"summary": "synthetic-first-key and synthetic-second-key"}

    result = asyncio.run(engine._run(runner, {}, lambda *e: events.append(e), workflow=workflow))
    assert [r[0] for r in requests] == ["first.example"] * (len(roles)-1) + ["second.example"]
    assert requests[-1][1] == "Bearer synthetic-second-key"
    assert requests[-1][2]["model"] == "specialized-model"
    assert requests[-1][2]["reasoning_effort"] == "high"
    assert all(r[1] == "Bearer synthetic-first-key" for r in requests[:-1])
    assert "synthetic-" not in result["summary"]
    assert result["agent_models"][roles[-1]]["base_url"] == "https://second.example/v1"
    assert result["routing_version"] < providers.state()["version"]
    assert model_identity(engine)[-1] == providers.state()["version"]
    assert len(clients) == 2 and all(client.is_closed for _, client in clients)
    assert "synthetic-second-key" not in json.dumps(events)


class RoleProbe(Model):
    def __init__(self, delegate, role, seen, effort):
        self.delegate, self.role, self.seen = delegate, role, seen
        self.reasoning_effort = effort

    async def get_response(self, *args, **kwargs):
        self.seen.append(self.role)
        actual = kwargs["model_settings"].reasoning
        assert (actual.effort if actual else None) == self.reasoning_effort
        return await self.delegate.get_response(*args, **kwargs)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


@pytest.mark.parametrize("workflow", ["analysis", "repair", "diagnosis"])
def test_native_sdk_graphs_use_each_role_delegate_and_reasoning(tmp_path, workflow):
    roles = [key.split(".")[1] for key in AGENT_ROLES if key.startswith(workflow + ".")]
    shared = {"analysis": ReviewModel(), "repair": RepairModel(),
              "diagnosis": DiagnosticModel()}[workflow]
    seen = []
    shared.role_models = {role: RoleProbe(shared, role, seen, "high" if i else None)
                          for i, role in enumerate(roles)}
    if workflow == "analysis":
        result = asyncio.run(run_analysis(shared, {"files": {"app.py": "print('ok')"},
            "config": {}, "jobs": [], "test_result": None}, lambda *_: None))
    elif workflow == "diagnosis":
        result = asyncio.run(run_agents(shared, {
            "log": UNKNOWN_LOG, "manifests": {}, "config": {},
        }))
    else:
        source = tmp_path / "environment/source"
        source.mkdir(parents=True)
        (source / "app.py").write_bytes(b"print('ok')")
        root = tmp_path / "repairs"
        root.mkdir()
        execution = SimpleNamespace(root=root, directory=lambda _: source.parent,
                                    runtime=RepairRuntime())
        workspace = RepairWorkspace(execution, "repair_" + "b" * 16,
            {"id": "synthetic", "config": {"language": "python", "test": "suite"}}, lambda: None)
        result = asyncio.run(run_repair(shared, {"finding": {}, "confirmation": {}},
                                       lambda *_: None, workspace=workspace))
    assert set(seen) == set(roles) and result["handoffs"]


def test_evaluation_rejects_different_role_models_even_when_default_matches():
    first = {"report": {"model": "same", "routing_version": 4, "agent_models": {"a": "first"}}}
    second = {"report": {"model": "same", "routing_version": 4, "agent_models": {"a": "other"}}}
    assert not _model_consistent(first, second)


def test_selected_deepseek_role_keeps_its_own_handoff_policy(catalog):
    providers, engine = catalog
    saved = create(providers)
    saved = create(providers, name="DeepSeek", base_url="https://api.deepseek.com/v1")
    configure(providers, {"default": choice(saved),
                          "analysis.problem_investigator": choice(saved, index=1)})

    async def runner(model, *args, **kwargs):
        assert not model.retry_handoff_mismatch
        chosen = model_for_role(model, "problem_investigator")
        assert chosen.retry_handoff_mismatch and chosen.reasoning_effort is None
        assert not model_for_role(model, "independent_reviewer").retry_handoff_mismatch
        return {}

    asyncio.run(engine._run(runner, {}, lambda *_: None))


def test_handoff_retry_is_selected_per_role_in_the_native_graph(monkeypatch):
    from repowatch.repository_analysis import STAGE_LIMITS

    for role, turns in zip(("repository_analyst", "problem_investigator",
                            "reproduction_assistant", "independent_reviewer"), (2, 0, 0, 0)):
        monkeypatch.setitem(STAGE_LIMITS, role, (turns, 10, 25000))
    shared, events = ReviewModel(), []

    class HandoffProbe(RoleProbe):
        retry_handoff_mismatch = True
        rejected = False

        async def get_response(self, *args, **kwargs):
            response = await super().get_response(*args, **kwargs)
            if not self.rejected:
                self.rejected = True
                response.output[0] = response.output[0].model_copy(
                    update={"name": "unexpected_handoff"},
                )
            return response

    shared.role_models = {"problem_investigator": HandoffProbe(
        shared, "problem_investigator", [], None,
    )}
    report = asyncio.run(run_analysis(shared, {"files": {"app.py": "print('ok')"},
        "config": {}, "jobs": [], "test_result": None}, lambda *e: events.append(e)))
    assert len(report["handoffs"]) == 3
    assert events.count(("handoff_retry", "problem_investigator")) == 1


def test_stale_legacy_editor_cannot_overwrite_a_new_default_selection(catalog):
    providers, engine = catalog
    legacy = engine.legacy_status()
    saved = create(providers)
    configure(providers, {"default": choice(saved)})
    with pytest.raises(Conflict, match="model_providers_changed"):
        engine.save(version=0, routing_version=legacy["routing_version"], enabled=True,
                    model="stale", api="responses", base_url="https://old.example/v1",
                    api_key="old-key")
    assert engine.status()["model"] == "general-model"


@pytest.mark.parametrize("cancel", [False, True])
def test_all_clients_close_when_workflow_raises_or_is_cancelled(catalog, monkeypatch, cancel):
    providers, engine = catalog
    saved = create(providers)
    saved = create(providers, name="供应商乙", base_url="https://second.example/v1")
    configure(providers, {"default": choice(saved),
                          "analysis.independent_reviewer": choice(saved, index=1)})
    clients = []

    def client(**kwargs):
        opened = AsyncOpenAI(**kwargs)
        clients.append(opened)
        return opened

    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", client)

    async def runner(*args, **kwargs):
        raise asyncio.CancelledError if cancel else RuntimeError("synthetic stop")

    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        asyncio.run(engine._run(runner, {}, lambda *_: None))
    assert len(clients) == 2 and all(c.is_closed() for c in clients)


def test_echoed_escaped_short_keys_are_redacted_without_corrupting_output_fields(catalog):
    providers, engine = catalog
    secret = 'a"\\b'
    saved = create(providers, api_key=secret)
    configure(providers, {"default": choice(saved)})

    async def runner(*args, **kwargs):
        return {"approved": True, "summary": secret, "nested": [secret]}

    result = asyncio.run(engine._run(runner, {}, lambda *_: None))
    assert result["approved"] and result["summary"] == "[REDACTED]"
    assert result["nested"] == ["[REDACTED]"]


def test_provider_api_requires_owner_csrf_and_never_echoes_secret(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False, credential_vault=MemoryVault())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/model-providers").status_code == 401
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        payload = {"version": 0, **provider()}
        assert client.post("/api/model-providers", json=payload).status_code == 403
        invalid = client.post("/api/model-providers/import", headers=headers, json={
            "version": 0, "providers": [{**provider(key="private-import-key"), "extra": True}],
        })
        assert invalid.status_code == 422 and "private-import-key" not in invalid.text
        missing_name = provider(key="private-missing-field-key")
        missing_name.pop("name")
        invalid = client.post("/api/model-providers/import", headers=headers,
                              json={"version": 0, "providers": [missing_name]})
        assert invalid.status_code == 422 and "private-missing-field-key" not in invalid.text
        response = client.post("/api/model-providers", headers=headers, json=payload)
        assert response.status_code == 200 and "synthetic-first-key" not in response.text
        saved = response.json()
        selected = {"version": saved["version"], "enabled": True,
                    "routes": {"default": choice(saved), "repair.repair_agent": choice(saved)}}
        assert client.post("/api/agent-models", json=selected).status_code == 403
        assert client.post("/api/agent-models", headers=headers, json=selected).status_code == 200
        assert app.state.analysis_model.status()["enabled"]
        assert client.post(f"/api/model-providers/{choice(saved)['provider_id']}/delete",
            headers=headers, json={"version": saved["version"]+1}).status_code == 409
