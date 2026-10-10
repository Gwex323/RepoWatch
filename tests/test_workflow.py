import asyncio
import json
import mimetypes
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from repowatch import fixture
from repowatch.app import ROOT, create_app
from repowatch.store import Conflict, Store
from repowatch.workflow import ROLES, Worker, Workflow


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


def tick(store):
    assert asyncio.run(Worker(store).tick())


def waiting(store):
    run_id = store.start_run("repo_fixture", reuse=False)
    tick(store)
    run = store.run(run_id)
    assert run["status"] == "waiting", run["error"]
    return run_id


def approve(store, run_id):
    store.observe(run_id, 20, 3, 10)
    store.confirm(run_id, "confirmed", fixture.REVISION, "Personally reproduced.")


def test_native_handoffs_and_persistent_interruption(store):
    run_id = waiting(store)
    details = store.details(run_id)
    transfers = [e for e in details["events"] if e["kind"] == "handoff"]
    assert len(transfers) == 2
    assert transfers[0]["message"] == "repository_analyst -> problem_investigator"
    assert transfers[1]["message"] == "problem_investigator -> reproduction_assistant"
    assert details["has_snapshot"]
    assert details["artifacts"] == []
    assert "snapshot" not in details
    with pytest.raises(Conflict):
        store.artifact(run_id, "pr_draft", {"published": True})


def test_restart_in_another_process_and_resume(store):
    run_id = waiting(store)
    approve(store, run_id)
    result = subprocess.run(
        [sys.executable, "-m", "repowatch.worker", "--database", str(store.path), "--once"],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    details = Store(store.path).details(run_id)
    assert details["status"] == "demo_completed", details["error"]
    assert len([e for e in details["events"] if e["kind"] == "handoff"]) == 5
    assert {e["agent"] for e in details["events"] if e["kind"] == "agent"} == set(ROLES)
    assert {a["kind"] for a in details["artifacts"]} == {
        "issue_draft",
        "patch_draft",
        "verification",
        "pr_draft",
    }
    assert details["result"]["published"] is False


def test_duplicate_confirmation_creates_one_resume_job(store):
    run_id = waiting(store)
    approve(store, run_id)
    assert store.confirm(run_id, "confirmed", fixture.REVISION, "Repeated click") is False
    tick(store)
    assert asyncio.run(Worker(store).tick()) is False
    assert len(store.details(run_id)["artifacts"]) == 4


def test_confirmation_is_scoped_to_one_finding(store):
    a, b = waiting(store), waiting(store)
    approve(store, a)
    tick(store)
    assert store.run(a)["status"] == "demo_completed"
    assert store.run(b)["status"] == "waiting"
    assert store.details(b)["artifacts"] == []


def test_unreproduced_and_stale_findings_cannot_continue(store):
    run_id = waiting(store)
    with pytest.raises(Conflict):
        store.confirm(run_id, "confirmed", fixture.REVISION, "")
    store.observe(run_id, 20, 1, 10)
    with pytest.raises(Conflict):
        store.confirm(run_id, "confirmed", fixture.REVISION, "")
    store.observe(run_id, 20, 3, 10)
    with pytest.raises(Conflict):
        store.confirm(run_id, "confirmed", "0" * 64, "")
    store.confirm(run_id, "not_reproduced", fixture.REVISION, "Cannot reproduce.")
    assert asyncio.run(Worker(store).tick()) is False
    with pytest.raises(Conflict):
        store.require_confirmation(run_id)
    with pytest.raises(Conflict):
        store.confirm(run_id, "confirmed", fixture.REVISION, "")
    assert store.details(run_id)["artifacts"] == []


def test_corrupt_snapshot_and_version_fail_closed(store):
    run_id = waiting(store)
    approve(store, run_id)
    with store.connection() as db:
        db.execute("UPDATE runs SET snapshot='{}' WHERE id=?", (run_id,))
    tick(store)
    assert store.run(run_id)["status"] == "failed"
    assert store.details(run_id)["artifacts"] == []
    assert "integrity" in store.run(run_id)["error"]


def test_negative_case_fixture_and_patch_checks():
    assert fixture.experiment(20, 3, 10)["reproduced"]
    assert not fixture.experiment(20, 1, 10)["reproduced"]
    assert fixture.verify_patch()["passed"] == 5


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def login(client):
    result = client.post("/api/setup", json={"password": "local-test-password"})
    assert result.status_code == 200
    return {"X-CSRF-Token": result.json()["csrf"]}


def test_auth_origin_csrf_and_no_snapshot_exposure(client):
    assert client.get("/api/dashboard").status_code == 401
    headers = login(client)
    assert client.post("/api/repositories/repo_fixture/runs").status_code == 403
    assert (
        client.post(
            "/api/repositories/repo_fixture/runs",
            headers={**headers, "Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    result = client.post("/api/repositories/repo_fixture/runs", headers=headers)
    assert result.status_code == 200
    run_id = result.json()["run_id"]
    tick(client.app.state.store)
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert "snapshot" not in detail.json()
    assert detail.headers["cache-control"] == "no-store"
    assert (
        client.post(
            f"/api/runs/{run_id}/confirmation",
            headers=headers,
            json={
                "decision": "confirmed",
                "revision": fixture.REVISION,
                "personally_reproduced": True,
                "state": {},
            },
        ).status_code
        == 422
    )


def test_api_confirmation_and_blocked_external_execution(client):
    headers = login(client)
    result = client.post(
        "/api/repositories",
        headers=headers,
        json={"url": "https://github.com/openai/openai-agents-python"},
    )
    assert result.status_code == 201
    repo_id = result.json()["id"]
    assert client.post(f"/api/repositories/{repo_id}/runs", headers=headers).status_code == 409
    assert (
        client.post(
            "/api/repositories", headers=headers, json={"url": "file:///etc/passwd"}
        ).status_code
        == 422
    )
    run_id = client.post("/api/repositories/repo_fixture/runs", headers=headers).json()["run_id"]
    tick(client.app.state.store)
    assert client.post(
        f"/api/runs/{run_id}/experiment", headers=headers, json={"total": 20, "page": 3, "size": 10}
    ).json()["reproduced"]
    body = {
        "decision": "confirmed",
        "revision": fixture.REVISION,
        "personally_reproduced": True,
        "note": "Checked in the fixture panel.",
    }
    assert client.post(f"/api/runs/{run_id}/confirmation", headers=headers, json=body).json()[
        "changed"
    ]
    assert not client.post(f"/api/runs/{run_id}/confirmation", headers=headers, json=body).json()[
        "changed"
    ]
    tick(client.app.state.store)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "demo_completed"
    assert not client.get("/api/system").json()["github_publication"]


def test_password_login_and_logout(client):
    headers = login(client)
    assert client.post("/api/setup", json={"password": "another-password"}).status_code == 409
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert not client.get("/api/session").json()["authenticated"]
    assert client.post("/api/login", json={"password": "incorrect-password"}).status_code == 401
    assert client.post("/api/login", json={"password": "local-test-password"}).status_code == 200


def test_expired_lease_can_recover_and_claim_is_exclusive(store):
    run_id = store.start_run("repo_fixture")
    first = store.claim_job("first")
    assert first["run_id"] == run_id
    assert store.claim_job("other") is None
    with store.connection() as db:
        db.execute("UPDATE jobs SET lease=0 WHERE id=?", (first["id"],))
    tick(store)
    assert store.run(run_id)["status"] == "waiting"
    store.finish_job(first["id"], "first", status="failed")
    with store.connection() as db:
        job = db.execute("SELECT status FROM jobs WHERE id=?", (first["id"],)).fetchone()
        assert job["status"] == "done"


def test_saved_state_is_json_not_pickle(store):
    run_id = waiting(store)
    private = store.run(run_id, private=True)
    assert isinstance(json.loads(private["snapshot"]), dict)
    assert "api_key" not in private["snapshot"]


def test_static_module_mime_overrides_windows_registry(tmp_path):
    mimetypes.add_type("text/plain", ".js")
    app = create_app(tmp_path / "mime.db", start_worker=False)
    assert mimetypes.guess_type("module.js")[0] == "text/javascript"
    assets = list((ROOT / "dist/assets").glob("*.js"))
    if not assets:
        pytest.skip("Build the frontend before checking served asset headers.")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(f"/assets/{assets[0].name}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("field", ["sdk_version", "graph_version", "revision"])
def test_version_changes_block_resume(store, field):
    run_id = waiting(store)
    approve(store, run_id)
    with store.connection() as db:
        # Field names come only from this fixed test parametrization.
        db.execute(f"UPDATE runs SET {field}='changed' WHERE id=?", (run_id,))
    tick(store)
    assert store.run(run_id)["status"] == "failed"
    assert store.details(run_id)["artifacts"] == []


def test_snapshot_cannot_be_transplanted_to_another_finding(store):
    first, second = waiting(store), waiting(store)
    approve(store, first)
    other = store.run(second, private=True)
    with store.connection() as db:
        db.execute(
            "UPDATE runs SET snapshot=?,snapshot_hash=? WHERE id=?",
            (other["snapshot"], other["snapshot_hash"], first),
        )
    tick(store)
    assert store.run(first)["status"] == "failed"
    assert store.run(second)["status"] == "waiting"
    assert store.details(first)["artifacts"] == []
    assert store.details(second)["artifacts"] == []


def test_no_agent_has_merge_or_remote_publication_tool(store):
    workflow = Workflow(store)
    assert set(workflow.agents) == set(ROLES)
    names = {tool.name for agent in workflow.agents.values() for tool in agent.tools}
    assert names == {
        "record_evidence",
        "human_reproduction",
        "prepare_fixture_patch",
        "review_fixture_patch",
        "prepare_pr_draft",
    }
    for current, following in zip(ROLES[:-1], ROLES[1:]):
        assert workflow.agents[current].handoffs == [workflow.agents[following]]
    assert workflow.agents[ROLES[-1]].handoffs == []
