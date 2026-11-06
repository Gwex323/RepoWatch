import json
import mimetypes
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def api(tmp_path):
    def no_network(*_args, **_kwargs):
        raise AssertionError("管理 API 不允许隐式推理或远端操作")

    values = {}
    vault = SimpleNamespace(
        create=lambda r, s: values.update({r: s}),
        read=lambda r: values[r],
        delete=lambda r: values.pop(r, None),
    )
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    services = HostServices(
        settings, vault=vault, clock=lambda: 0, transport=httpx.MockTransport(no_network)
    )
    sessions = LocalSessions(clock=lambda: 0)
    client = TestClient(
        create_app(services, sessions=sessions),
        base_url=ORIGIN,
        client=("127.0.0.1", 5000),
        raise_server_exceptions=False,
    )
    with client:
        response = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": ORIGIN,
                "Authorization": "Bearer " + sessions.bootstrap_token,
            },
        )
        assert response.status_code == 200
        headers = {"Origin": ORIGIN, "X-RepoWatch-Session": response.json()["csrf"]}
        yield client, headers, services, sessions
    services.close()


def mutation(key, revision=None, **data):
    return {"operation_id": key, "expected_revision": revision, "data": data}


def test_task_artifact_read_is_scoped_and_has_no_execution_side_effect(api):
    client, _, services, _ = api
    repository = services.tasks.register_repository("artifact-view", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repository, "task:logs", now=0)
    other = services.tasks.ensure_task(repository, "task:other", now=0)
    content = b"TARGET_BUG\nAssertionError: sum differs\n"
    with services.store.write() as records:
        records.add(
            "runs",
            {
                "id": "log-run",
                "task_id": task,
                "budget_owner_task_id": task,
                "generation": 1,
                "snapshot_json": "{}",
                "state": "ended",
            },
        )
        records.add(
            "artifacts",
            {
                "id": "log-artifact",
                "run_id": "log-run",
                "sha256": sha256(content).hexdigest(),
                "relative_path": "fixture-log.txt",
                "context_json": "{}",
            },
        )
    services.artifacts.root.mkdir(parents=True, exist_ok=True)
    (services.artifacts.root / "fixture-log.txt").write_bytes(content)
    assert client.get(f"/api/v1/tasks/{other}/artifacts/log-artifact").status_code == 403
    response = client.get(f"/api/v1/tasks/{task}/artifacts/log-artifact")
    assert response.status_code == 200
    assert response.json()["content"] == content.decode()
    assert response.json()["truncated"] is False
    with services.store.read() as records:
        assert len(records.find("runs")) == 1
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_sse_reconnect_uses_durable_cursor_without_mutations_and_rechecks_session(api):
    client, _, services, sessions = api
    services.tasks.register_repository("sse-fixture", admission_mode="manual")
    before = client.get("/api/v1/state").json()
    response = client.get("/api/v1/events/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids and ids == sorted(ids)
    reconnect = client.get("/api/v1/events/stream?after=0", headers={"Last-Event-ID": str(ids[-1])})
    assert "event: business" not in reconnect.text
    assert client.get("/api/v1/state").json() == before
    assert client.get("/api/v1/events/stream", headers={"Last-Event-ID": "bad"}).status_code == 422
    sessions.sessions.clear()
    assert client.get("/api/v1/events/stream").status_code == 401


def test_unauthorized_origin_host_peer_and_session_mutations_are_rejected(api):
    client, headers, _, sessions = api
    request = mutation("pause", 1)
    assert client.post("/api/v1/control/pause", json=request).status_code == 403
    assert (
        client.post(
            "/api/v1/control/pause",
            headers={**headers, "Origin": "https://evil.example"},
            json=request,
        ).status_code
        == 403
    )
    assert client.get("/api/v1/state", headers={"Host": "evil.example"}).status_code == 403
    assert (
        client.post(
            "/api/v1/session/bootstrap",
            headers={"Origin": ORIGIN, "Authorization": "Bearer " + sessions.bootstrap_token},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/v1/state").status_code == 401
    remote = TestClient(client.app, base_url=ORIGIN, client=("192.0.2.10", 4000))
    with remote:
        assert remote.get("/api/v1/health").status_code == 403


def test_repeat_control_operation_returns_original_and_stale_or_changed_request_conflicts(api):
    client, headers, _, _ = api
    body = mutation("pause", 1)
    response = client.post("/api/v1/control/pause", json=body, headers=headers)
    assert response.status_code == 200
    assert (
        client.post("/api/v1/control/pause", json=body, headers=headers).json() == response.json()
    )
    assert (
        client.post("/api/v1/control/pause", json=mutation("other", 1), headers=headers).status_code
        == 409
    )
    assert client.post("/api/v1/control/resume", json=body, headers=headers).status_code == 409


def test_provider_configuration_events_errors_and_api_validation_never_echo_key(api):
    client, headers, _, _ = api
    secret = "test-api-key-8675309"
    payload = {
        "name": "api",
        "base_url": "https://unused.example/v1",
        "api": "responses",
        "api_key": secret,
        "models": [],
    }
    saved = client.post(
        "/api/v1/providers/import", json=mutation("save", payload=payload), headers=headers
    )
    assert saved.status_code == 200
    for path in ("/api/v1/providers", "/api/v1/events", "/api/v1/state"):
        response = client.get(path)
        assert response.status_code == 200
        assert secret not in response.text
    bad = client.post(
        "/api/v1/providers/import",
        json=mutation("bad", payload={**payload, "name": "other", "api_key": ""}),
        headers=headers,
    )
    assert bad.status_code == 422 and secret not in bad.text
    malformed = client.post(
        "/api/v1/control/pause",
        json={"operation_id": secret, "expected_revision": secret, "data": {}},
        headers=headers,
    )
    assert malformed.status_code == 422 and secret not in malformed.text
    assert client.get("/api/v1/credentials").status_code == 404


def test_refresh_and_diagnostic_request_do_not_start_work_and_pause_remains(api):
    client, headers, services, _ = api
    body = mutation(
        "diag",
        1,
        kind="storage_probe",
        parameters={},
        limits={
            "active_ms": 10000,
            "model_calls": 1,
            "tokens": 1000,
            "repair_rounds": 1,
            "recovery_rounds": 1,
        },
        duration=10,
    )
    result = client.post("/api/v1/diagnostics", json=body, headers=headers)
    assert result.status_code == 200
    assert client.post("/api/v1/diagnostics", json=body, headers=headers).json() == result.json()
    for _ in range(2):
        state = client.get("/api/v1/state").json()
        assert state["control"]["paused"] == 1
        assert len(state["tasks"]) == 1
        assert state["tasks"][0]["state"] == "queued"
    with services.store.read() as records:
        assert records.find("runs") == []
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None


def test_task_information_update_deduplicates_and_events_reconnect(api):
    client, headers, services, _ = api
    repo = services.tasks.register_repository("repo", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", now=0)
    body = mutation("info", 1, action="supply_information", payload={"steps": "实际复现步骤"})
    first = client.post(f"/api/v1/tasks/{task}/operate", json=body, headers=headers)
    assert first.status_code == 200
    assert (
        client.post(f"/api/v1/tasks/{task}/operate", json=body, headers=headers).json()
        == first.json()
    )
    detail = client.get(f"/api/v1/tasks/{task}")
    assert detail.status_code == 200
    assert detail.json()["input"]["human_context"]["steps"] == "实际复现步骤"
    events = client.get("/api/v1/events?limit=2").json()
    continuation = client.get(f"/api/v1/events?after={events['cursor']}").json()
    assert all(row["sequence"] > events["cursor"] for row in continuation["events"])
    assert "actual-secret" not in json.dumps(detail.json())


def test_acceptance_api_replays_approval_and_exposes_stale_input_without_starting_worker(api):
    client, headers, services, _ = api
    repo = services.tasks.register_repository("acceptance-api", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:acceptance", issue_id="acceptance", now=0)
    specification = mutation(
        "acceptance-spec",
        1,
        action="set_acceptance",
        payload={
            "kind": "feature",
            "content": {
                "objective": "display status",
                "plan": "add read-only view",
                "criteria": ["status visible"],
            },
        },
    )
    response = client.post(f"/api/v1/tasks/{task}/operate", json=specification, headers=headers)
    assert response.status_code == 200
    approval = mutation(
        "acceptance-approval", response.json()["revision"], action="approve_acceptance"
    )
    approved = client.post(f"/api/v1/tasks/{task}/operate", json=approval, headers=headers)
    assert approved.status_code == 200
    assert (
        client.post(f"/api/v1/tasks/{task}/operate", json=approval, headers=headers).json()
        == approved.json()
    )
    detail = client.get(f"/api/v1/tasks/{task}").json()
    assert detail["acceptance"]["approved"] and detail["acceptance"]["current"]
    assert detail["mode"] == "triage"
    services.tasks.observe(task, "issue:acceptance", "v2", {"body": "new scope"}, now=1)
    stale = client.get(f"/api/v1/tasks/{task}").json()
    assert not stale["acceptance"]["approved"] and not stale["acceptance"]["current"]
    with services.store.read() as records:
        assert not records.find("runs")
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_manual_budget_adjustment_without_run_keeps_pause_usage_and_blockers(api):
    client, headers, services, _ = api
    repo = services.tasks.register_repository("repo", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", now=0)
    initial = client.post(
        f"/api/v1/tasks/{task}/budget/initial",
        headers=headers,
        json=mutation(
            "budget",
            1,
            limits={
                "active_ms": 1000,
                "model_calls": 2,
                "tokens": 1000,
                "repair_rounds": 2,
                "recovery_rounds": 2,
            },
            reason="初始有限授权",
        ),
    )
    assert initial.status_code == 200
    with services.store.write() as records:
        records.add(
            "block_reasons",
            {"id": "manual-block", "task_id": task, "code": "no_progress", "details_json": "{}"},
        )
        records.change(
            "budget_ledgers",
            {"owner_task_id": task},
            {"used_json": '{"tokens":100}', "reserved_json": '{"tokens":200}'},
        )
    basis = client.get(f"/api/v1/tasks/{task}/budget/adjustment-basis").json()
    body = mutation(
        "add",
        basis["ledger"]["revision"],
        increase={"tokens": 500},
        basis_sha256=basis["basis_sha256"],
        reason="补充处理方案后人工有限加额",
    )
    result = client.post(f"/api/v1/tasks/{task}/budget/adjust", headers=headers, json=body)
    assert result.status_code == 200
    assert result.json()["ledger"]["used"]["tokens"] == 100
    assert result.json()["ledger"]["reserved"]["tokens"] == 200
    assert result.json()["ledger"]["limits"]["tokens"] == 1500
    assert (
        client.post(f"/api/v1/tasks/{task}/budget/adjust", headers=headers, json=body).json()
        == result.json()
    )
    assert (
        client.post(
            f"/api/v1/tasks/{task}/budget/adjust",
            headers=headers,
            json={**body, "operation_id": "stale"},
        ).status_code
        == 409
    )
    with services.store.read() as records:
        assert records.find("runs") == []
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert records.one("block_reasons", id="manual-block")["resolved"] == 0


def test_static_modules_keep_javascript_type_even_with_bad_host_mapping(api, tmp_path, monkeypatch):
    _, _, services, _ = api
    (tmp_path / "app.js").write_text("console.log('fixture')", encoding="utf-8")
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    client = TestClient(
        create_app(services, sessions=LocalSessions(), static_dir=tmp_path),
        base_url=ORIGIN,
        client=("127.0.0.1", 4000),
    )
    with client:
        response = client.get("/app.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.headers["x-content-type-options"] == "nosniff"
