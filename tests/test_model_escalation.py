"""Offline tests for the default-off model escalation policy."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from test_github_accounts import MemoryVault

from repowatch.analysis_model import AnalysisModel
from repowatch.app import create_app
from repowatch.github_accounts import GitHubAccounts
from repowatch.model_escalation import ModelEscalation
from repowatch.model_providers import ModelProviders
from repowatch.roles import ROLES
from repowatch.store import Conflict, Store


@pytest.fixture
def catalog(tmp_path):
    store = Store(tmp_path / "escalation.db")
    credentials = GitHubAccounts(store, vault=MemoryVault())
    providers = ModelProviders(store, credentials)
    return store, providers, ModelEscalation(store)


def saved(providers):
    return providers.save(
        version=providers.state()["version"],
        name="供应商甲",
        base_url="https://first.example/v1",
        api="chat_completions",
        api_key="synthetic-first-key",
        models=[
            {"model": "flash-monitor", "reasoning_effort": "low"},
            {"model": "v4pro-repair", "reasoning_effort": "high"},
        ],
    )


def route(state, model):
    return {"provider_id": state["providers"][0]["id"], "model": model}


def configure(providers, monitor, repair):
    return providers.configure(
        version=providers.state()["version"],
        enabled=True,
        routes={"default": monitor, "repair_agent": repair},
    )


def enabled_escalation(catalog):
    store, providers, escalation = catalog
    state = saved(providers)
    monitor = route(state, "flash-monitor")
    repair = route(state, "v4pro-repair")
    configure(providers, monitor, repair)
    escalation.configure(
        version=escalation.state()["version"],
        enabled=True,
        monitor_route=monitor,
        repair_route=repair,
    )
    return store, providers, escalation, monitor, repair


def insert_repair_task(store, repair_id="repair_x", status="running", revision="revision-1"):
    with store.connection() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            """
            INSERT OR REPLACE INTO repair_tasks(
                id,analysis_id,finding_id,env_id,repo_id,revision,status,stage,
                attempt,lease,context,version,created,updated
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                repair_id,
                "analysis_x",
                "finding_x",
                "env_x",
                "repo_x",
                revision,
                status,
                "repair_agent",
                1,
                0.0,
                "{}",
                1,
                time.time(),
                time.time(),
            ),
        )


def repair_context(repair_id="repair_x", status="running", revision="revision-1"):
    return {
        "_repair_task_id": repair_id,
        "_model_escalation_authorized": True,
        "revision": revision,
        "repair_task_status": status,
    }


class DummyOpenAI:
    """Stand-in transport that never opens a socket."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return False


def test_policy_is_disabled_by_default_and_does_not_change_original_routes(catalog):
    store, providers, escalation = catalog
    state = saved(providers)
    monitor = route(state, "flash-monitor")
    repair = route(state, "v4pro-repair")
    configure(providers, monitor, repair)
    with store.connection() as db:
        rows = ModelProviders.role_rows(db)
        applied, info = ModelEscalation.apply(db, rows, flow="repair", context={})
    assert info is None
    assert applied[ROLES[3]]["selection_choice"] == repair


def test_enable_requires_existing_provider_model_and_key(catalog):
    store, providers, escalation = catalog
    state = saved(providers)
    monitor = route(state, "flash-monitor")
    repair = route(state, "v4pro-repair")
    with pytest.raises(Conflict, match="model_provider_missing"):
        escalation.configure(
            version=escalation.state()["version"],
            enabled=True,
            monitor_route={"provider_id": "missing", "model": "flash-monitor"},
            repair_route=repair,
        )
    with pytest.raises(Conflict, match="model_provider_model_missing"):
        escalation.configure(
            version=escalation.state()["version"],
            enabled=True,
            monitor_route=monitor,
            repair_route={"provider_id": monitor["provider_id"], "model": "missing"},
        )
    assert not escalation.state()["enabled"]


def test_state_never_exposes_config_or_encrypted_key(catalog):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    serialized = json.dumps(escalation.state())
    assert "encrypted_key" not in serialized
    assert "synthetic-first-key" not in serialized
    assert "base_url" not in serialized
    assert escalation.state()["monitor"]["model"] == monitor["model"]
    assert escalation.state()["repair"]["model"] == repair["model"]


def test_state_hides_unresolvable_model_identity(catalog):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    with store.connection() as db:
        db.execute(
            "UPDATE model_escalation_policy SET monitor_route=? WHERE id=1",
            (json.dumps({"provider_id": "missing", "model": "missing-model"}),),
        )
    state = escalation.state()
    assert state["monitor"] is None
    assert state["missing"] == ["model_provider_missing"]
    assert state["monitor_route"]["model"] == "missing-model"


def test_role_split_and_maintenance_never_escalate(catalog):
    store, providers, escalation = catalog
    state = saved(providers)
    monitor = route(state, "flash-monitor")
    repair = route(state, "v4pro-repair")
    configure(providers, monitor, repair)
    escalation.configure(
        version=escalation.state()["version"],
        enabled=True,
        monitor_route=monitor,
        repair_route=repair,
    )
    insert_repair_task(store)
    with store.connection() as db:
        rows = ModelProviders.role_rows(db)
        applied, info = ModelEscalation.apply(
            db, rows, flow="repair", context=repair_context(),
        )
        assert info["repair_model_used"] is True
        for role, row in applied.items():
            expected = repair["model"] if role == "repair_agent" else monitor["model"]
            assert json.loads(row["config"])["model"] == expected

        maintenance, maintenance_info = ModelEscalation.apply(
            db, rows, flow="repair", context={"_maintenance_classification": True},
            force_monitor=True,
        )
        assert maintenance_info["repair_model_used"] is False
        assert json.loads(maintenance["repair_agent"]["config"])["model"] == monitor["model"]


def test_server_flag_alone_cannot_authorize_a_repair(catalog):
    store, _, escalation, _, _ = enabled_escalation(catalog)
    with store.connection() as db:
        assert not ModelEscalation.repair_authorized(
            db, {"_model_escalation_authorized": True},
        )
    insert_repair_task(store, status="running")
    with store.connection() as db:
        assert not ModelEscalation.repair_authorized(
            db, {"_model_escalation_authorized": True},
        )
        assert ModelEscalation.repair_authorized(
            db, repair_context(),
        )


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_negative_routing_never_downgrades_repair_to_monitor(
    catalog, status,
):
    store, providers, escalation, _, _ = enabled_escalation(catalog)
    insert_repair_task(store, status=status)
    context = repair_context(status=status)

    with store.connection() as db:
        assert not ModelEscalation.repair_authorized(db, context)
        with pytest.raises(Conflict, match="model_escalation_authorization_required"):
            ModelEscalation.apply(db, ModelProviders.role_rows(db), flow="repair", context=context)

    engine = AnalysisModel(store, providers.credentials)
    events = []
    called = False

    async def runner(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    with pytest.raises(Conflict, match="model_escalation_authorization_required"):
        asyncio.run(engine._run(
            runner, context, lambda *event: events.append(event), flow="repair",
        ))
    assert called is False
    unavailable = next(value for kind, value in events if kind == "model_escalation_unavailable")
    assert json.loads(unavailable)["reason"] == "model_escalation_authorization_required"


def test_offline_workflow_uses_repair_model_only_for_authorized_repair_then_returns_monitor(
    catalog, monkeypatch,
):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    insert_repair_task(store)
    engine = AnalysisModel(store, providers.credentials)
    DummyOpenAI.instances.clear()
    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", DummyOpenAI)
    events = []

    async def runner(model, context, event, *, reasoning_effort=None, **options):
        # A repair runner is a pure handoff in this offline check: no provider request.
        return {"handoffs": ["repair_agent -> independent_reviewer"], "approved": True}

    result = asyncio.run(engine._run(
        runner,
        repair_context(),
        lambda *event: events.append(event),
        flow="repair",
    ))

    assert result["role_models"]["repair_agent"]["model"] == repair["model"]
    assert result["role_models"]["independent_reviewer"]["model"] == monitor["model"]
    assert result["model_escalation"]["repair_model_used"] is True
    event_kinds = [kind for kind, _ in events]
    assert "model_escalation_policy" in event_kinds
    assert "model_escalation_upgraded" in event_kinds
    assert "model_escalation_return" in event_kinds
    return_event = next(value for kind, value in events if kind == "model_escalation_return")
    assert json.loads(return_event)["reason"] == "repair_finished"
    assert all(client.closed for client in DummyOpenAI.instances)


def test_analysis_recovery_identity_includes_effective_escalation_route(
    catalog, monkeypatch,
):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    engine = AnalysisModel(store, providers.credentials)
    DummyOpenAI.instances.clear()
    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", DummyOpenAI)
    identity = engine.recovery_identity()
    events = []

    async def runner(model, context, event, *, reasoning_effort=None, **options):
        return {"handoffs": ["independent_reviewer"]}

    contexts = (
        ({}, False),
        ({"revision": "revision-1"}, False),
        ({
            "_repair_task_id": "repair_x",
            "_model_escalation_authorized": True,
        }, True),
    )
    for extra_context, force_monitor in contexts:
        result = asyncio.run(engine._run(
            runner,
            {"_review_model_identity": identity, **extra_context},
            lambda *event: events.append(event),
            flow="analysis",
            force_monitor=force_monitor,
        ))
        assert all(
            value["model"] == monitor["model"]
            for value in result["role_models"].values()
        )
    assert all(client.closed for client in DummyOpenAI.instances)

    # A version-only policy change must also invalidate a saved recovery identity.
    escalation.configure(
        version=escalation.state()["version"],
        enabled=True,
        monitor_route=monitor,
        repair_route=repair,
    )
    versioned_identity = engine.recovery_identity()
    assert versioned_identity != identity

    # Route changes must invalidate recovery even when both routes stay valid.
    escalation.configure(
        version=escalation.state()["version"],
        enabled=True,
        monitor_route=repair,
        repair_route=monitor,
    )
    assert engine.recovery_identity() != versioned_identity

    with pytest.raises(Conflict, match="analysis_model_changed"):
        asyncio.run(engine._run(
            runner,
            {"_review_model_identity": versioned_identity},
            lambda *_: None,
            flow="analysis",
        ))


def test_analysis_recovery_identity_tracks_provider_key_and_model_config(catalog):
    store, providers, escalation, _, _ = enabled_escalation(catalog)
    engine = AnalysisModel(store, providers.credentials)
    identity = engine.recovery_identity()
    state = providers.state()
    provider = state["providers"][0]

    providers.save(
        version=state["version"],
        provider_id=provider["id"],
        name=provider["name"],
        base_url=provider["base_url"],
        api=provider["api"],
        api_key="synthetic-replacement-key",
        models=provider["models"],
    )
    key_identity = engine.recovery_identity()
    assert key_identity != identity

    state = providers.state()
    provider = state["providers"][0]
    models = [dict(model) for model in provider["models"]]
    models[0]["reasoning_effort"] = "medium"
    providers.save(
        version=state["version"],
        provider_id=provider["id"],
        name=provider["name"],
        base_url=provider["base_url"],
        api=provider["api"],
        api_key="",
        models=models,
    )
    assert engine.recovery_identity() != key_identity


def test_analysis_identity_uses_side_effect_free_canonical_analysis_routing(catalog):
    store, providers, escalation, _, _ = enabled_escalation(catalog)
    with store.connection() as db:
        db.execute("BEGIN")
        policy = ModelEscalation.policy_row(db)
        before = db.total_changes
        routed, info = ModelEscalation.apply_analysis(
            db, ModelProviders.role_rows(db), policy,
        )
        assert db.total_changes == before
    assert info["flow"] == "analysis"
    assert info["repair_model_used"] is False
    assert routed[ROLES[3]]["escalation_route"] == "monitor"


def test_analysis_recovery_identity_handles_missing_policy_row(catalog, monkeypatch):
    store, providers, escalation = catalog
    state = saved(providers)
    monitor = route(state, "flash-monitor")
    repair = route(state, "v4pro-repair")
    configure(providers, monitor, repair)
    engine = AnalysisModel(store, providers.credentials)
    configured_identity = engine.recovery_identity()
    with store.connection() as db:
        db.execute("DELETE FROM model_escalation_policy WHERE id=1")
    missing_identity = engine.recovery_identity()
    assert missing_identity != configured_identity

    DummyOpenAI.instances.clear()
    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", DummyOpenAI)

    async def runner(model, context, event, *, reasoning_effort=None, **options):
        return {"handoffs": ["independent_reviewer"]}

    asyncio.run(engine._run(
        runner,
        {"_review_model_identity": missing_identity},
        lambda *_: None,
        flow="analysis",
    ))
    assert all(client.closed for client in DummyOpenAI.instances)


def test_analysis_recovery_identity_without_escalation_is_stable(catalog):
    store, providers, escalation = catalog
    saved(providers)
    engine = AnalysisModel(store, providers.credentials)
    first = engine.recovery_identity()
    assert engine.recovery_identity() == first
    assert escalation.state()["enabled"] is False


def test_snapshot_freezes_phase_and_new_phase_takes_new_policy(catalog, monkeypatch):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    insert_repair_task(store)
    engine = AnalysisModel(store, providers.credentials)
    DummyOpenAI.instances.clear()
    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", DummyOpenAI)
    events = []
    original_version = escalation.state()["version"]

    async def runner(model, context, event, *, reasoning_effort=None, **options):
        # The first phase changes the policy after its own snapshot was captured.
        if escalation.state()["version"] == original_version:
            escalation.configure(
                version=original_version,
                enabled=True,
                monitor_route=repair,
                repair_route=monitor,
            )
        return {"handoffs": ["repair_agent -> independent_reviewer"]}

    first = asyncio.run(engine._run(
        runner,
        repair_context(),
        lambda *event: events.append(event),
        flow="repair",
    ))
    second = asyncio.run(engine._run(
        runner,
        repair_context(),
        lambda *event: events.append(event),
        flow="repair",
    ))

    assert first["model_escalation"]["version"] == original_version
    assert first["role_models"]["repair_agent"]["model"] == repair["model"]
    assert first["role_models"]["independent_reviewer"]["model"] == monitor["model"]
    assert second["model_escalation"]["version"] == original_version + 1
    assert second["role_models"]["repair_agent"]["model"] == monitor["model"]
    assert second["role_models"]["independent_reviewer"]["model"] == repair["model"]


@pytest.mark.parametrize(
    "runner_exc,expected",
    [
        (RuntimeError("synthetic secret stop"), "workflow_failed"),
        (asyncio.CancelledError(), "workflow_cancelled"),
        (Conflict("repair_tests_failed"), "repair_tests_failed"),
        (Conflict("repair_review_rejected"), "repair_review_rejected"),
    ],
)
def test_escalation_return_is_recorded_with_a_safe_code_on_failure(
    catalog, monkeypatch, runner_exc, expected,
):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    insert_repair_task(store)
    engine = AnalysisModel(store, providers.credentials)
    DummyOpenAI.instances.clear()
    monkeypatch.setattr("repowatch.analysis_model.AsyncOpenAI", DummyOpenAI)
    events = []

    async def runner(*args, **kwargs):
        raise runner_exc

    with pytest.raises(type(runner_exc)):
        asyncio.run(engine._run(
            runner, repair_context(), lambda *event: events.append(event), flow="repair",
        ))

    return_event = next(value for kind, value in events if kind == "model_escalation_return")
    assert json.loads(return_event)["reason"] == expected
    assert "synthetic secret stop" not in json.dumps(events)


@pytest.mark.parametrize("field", ["monitor_route", "repair_route"])
def test_unavailable_policy_reference_records_unavailable_and_keeps_failure(catalog, field):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    insert_repair_task(store)
    with store.connection() as db:
        db.execute(
            "UPDATE model_escalation_policy SET "
            f"{field}=? WHERE id=1",
            (json.dumps({"provider_id": "missing", "model": "missing-model"}),),
        )
    engine = AnalysisModel(store, providers.credentials)
    events = []

    async def runner(*args, **kwargs):
        return {"handoffs": ["repair_agent -> independent_reviewer"]}

    with pytest.raises(Conflict):
        asyncio.run(engine._run(
            runner, repair_context(), lambda *event: events.append(event), flow="repair",
        ))

    unavailable = next(value for kind, value in events if kind == "model_escalation_unavailable")
    payload = json.loads(unavailable)
    assert payload["reason"] in {"model_provider_missing", "model_provider_model_missing"}
    assert "missing-model" not in json.dumps(payload)


def test_repair_snapshot_is_persisted_and_not_mutated_by_later_policy_change(catalog):
    store, providers, escalation, monitor, repair = enabled_escalation(catalog)
    insert_repair_task(store)

    with store.connection() as db:
        rows = ModelProviders.role_rows(db)
        applied, info = ModelEscalation.apply(
            db, rows, flow="repair", context=repair_context(),
        )
        assert info["repair_model_used"] is True
        ModelEscalation.record_repair_snapshot(
            db, "repair_x", applied, info, context=repair_context(),
        )

    with store.connection() as db:
        stored_context = json.loads(
            db.execute("SELECT context FROM repair_tasks WHERE id='repair_x'").fetchone()[0]
        )
    snapshot = stored_context["model_escalation_snapshot"]
    assert snapshot["policy_version"] == escalation.state()["version"]
    assert snapshot["monitor"]["model"] == monitor["model"]
    assert snapshot["repair"]["model"] == repair["model"]
    assert snapshot["role_models"]["repair_agent"]["model"] == repair["model"]
    assert snapshot["role_models"]["repair_agent"]["escalation_route"] == "repair"
    assert "_model_escalation_authorized" not in stored_context
    assert "encrypted_key" not in json.dumps(snapshot)
    assert "synthetic-first-key" not in json.dumps(snapshot)

    escalation.configure(
        version=escalation.state()["version"],
        enabled=True,
        monitor_route=repair,
        repair_route=monitor,
    )
    with store.connection() as db:
        reloaded = json.loads(
            db.execute("SELECT context FROM repair_tasks WHERE id='repair_x'").fetchone()[0]
        )
    assert reloaded["model_escalation_snapshot"] == snapshot


def _api_client(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False, credential_vault=MemoryVault())
    client = TestClient(app, base_url="http://127.0.0.1")
    session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
    return client, session


def _api_provider(client, headers, *, key="synthetic-first-key", name="供应商甲"):
    state = client.get("/api/model-providers", headers=headers).json()
    response = client.post(
        "/api/model-providers",
        headers=headers,
        json={
            "version": state["version"],
            "name": name,
            "base_url": "https://first.example/v1",
            "api": "chat_completions",
            "api_key": key,
            "models": [
                {"model": "flash-monitor", "reasoning_effort": "low"},
                {"model": "v4pro-repair", "reasoning_effort": "high"},
            ],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_escalation_api_requires_login_csrf_and_same_origin(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False, credential_vault=MemoryVault())
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/model-escalation").status_code == 401
    session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
    headers = {"x-csrf-token": session["csrf"]}
    state = client.get("/api/model-escalation", headers=headers).json()
    payload = {"version": state["version"], "enabled": False,
               "monitor_route": None, "repair_route": None}
    assert client.post("/api/model-escalation", json=payload).status_code == 403
    assert client.post(
        "/api/model-escalation", headers=headers, json=payload,
    ).status_code == 200
    assert client.post(
        "/api/model-escalation",
        headers={**headers, "origin": "http://evil.example"},
        json=payload,
    ).status_code == 403
    assert client.post(
        "/api/model-escalation",
        headers=headers,
        json={**payload, "version": state["version"]},
    ).status_code == 409


def test_escalation_api_enables_valid_routes_and_rejects_missing_model_or_key(tmp_path):
    client, session = _api_client(tmp_path)
    headers = {"x-csrf-token": session["csrf"]}
    saved = _api_provider(client, headers)
    provider_id = saved["providers"][0]["id"]
    monitor = {"provider_id": provider_id, "model": "flash-monitor"}
    repair = {"provider_id": provider_id, "model": "v4pro-repair"}
    state = client.get("/api/model-escalation", headers=headers).json()
    response = client.post(
        "/api/model-escalation",
        headers=headers,
        json={"version": state["version"], "enabled": True,
              "monitor_route": monitor, "repair_route": repair},
    )
    assert response.status_code == 200 and response.json()["enabled"] is True

    missing_model = client.post(
        "/api/model-escalation",
        headers=headers,
        json={"version": response.json()["version"], "enabled": True,
              "monitor_route": {"provider_id": provider_id, "model": "missing"},
              "repair_route": repair},
    )
    assert missing_model.status_code == 409

    no_key = _api_provider(client, headers, key="", name="供应商乙")
    no_key_id = no_key["providers"][-1]["id"]
    state = client.get("/api/model-escalation", headers=headers).json()
    missing_key = client.post(
        "/api/model-escalation",
        headers=headers,
        json={"version": state["version"], "enabled": True,
              "monitor_route": {"provider_id": no_key_id, "model": "flash-monitor"},
              "repair_route": repair},
    )
    assert missing_key.status_code == 409
