import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from repowatch import fixture
from repowatch.app import create_app
from repowatch.memory import Memory
from repowatch.store import FIXTURE_ISSUE_REF, Conflict, Store
from repowatch.workflow import Worker, Workflow


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "policy.db")


def tick(store):
    assert asyncio.run(Worker(store).tick())


def test_existing_issue_automatically_validates_without_owner_or_duplicate_issue(store):
    run_id = store.start_issue_sample()
    tick(store)
    run = Store(store.path).details(run_id)
    assert run["status"] == "demo_completed", run["error"]
    assert run["source_kind"] == "existing_issue"
    assert run["issue_ref"] == FIXTURE_ISSUE_REF
    assert run["automatic_validation"]["decision"] == "verified"
    assert run["confirmation"] is None and run["observation"] is None
    assert not run["has_snapshot"]
    assert len([e for e in run["events"] if e["kind"] == "handoff"]) == 5
    assert not any(e["kind"] in ("human", "checkpoint", "resume") for e in run["events"])
    artifacts = {a["kind"]: a["content"] for a in run["artifacts"]}
    assert set(artifacts) == {"patch_draft", "verification", "pr_draft"}
    assert artifacts["verification"]["passed"] == 5
    assert artifacts["pr_draft"]["issue_ref"] == FIXTURE_ISSUE_REF
    assert not artifacts["pr_draft"]["published"] and not artifacts["pr_draft"]["merge_enabled"]
    memories = Memory(store).list("repo_fixture")["items"]
    assert len(memories) == 2
    bug = next(m for m in memories if m["category"] == "bug")
    assert bug["evidence"]["actor"] == "workflow"
    assert "owner confirmation" in bug["content"]
    with pytest.raises(Conflict):
        store.artifact(run_id, "issue_draft", {})
    with pytest.raises(Conflict):
        store.confirm(run_id, "confirmed", fixture.REVISION, "")
    with pytest.raises(Conflict):
        store.observe(run_id, 20, 3, 10)


def test_distinct_intakes_do_not_reuse_each_other_or_unlock_discovery(store):
    discovered = store.start_run("repo_fixture")
    issue = store.start_issue_sample()
    assert discovered != issue
    assert store.start_run("repo_fixture") == discovered
    assert store.start_issue_sample() == issue
    tick(store)
    tick(store)
    assert store.run(discovered)["status"] == "waiting"
    assert store.run(issue)["status"] == "demo_completed"
    with pytest.raises(Conflict):
        store.require_reproduction(discovered)
    with pytest.raises(Conflict):
        store.validate_issue_sample(discovered)
    with pytest.raises(Conflict):
        store.save_waiting(issue, "{}")


def test_automatic_failure_pauses_without_human_gate_and_can_retry(store, monkeypatch):
    experiment = fixture.experiment
    monkeypatch.setattr(fixture, "experiment", lambda *args: experiment(20, 1, 10))
    run_id = store.start_issue_sample()
    tick(store)
    run = store.details(run_id)
    assert run["status"] == "validation_blocked", run["error"]
    assert run["automatic_validation"]["decision"] == "failed"
    assert run["artifacts"] == [] and not run["has_snapshot"]
    assert run["confirmation"] is None
    assert any(e["kind"] == "validation_blocked" for e in run["events"])
    assert Memory(store).list("repo_fixture")["items"] == []
    with pytest.raises(Conflict):
        store.require_reproduction(run_id)
    monkeypatch.setattr(fixture, "experiment", experiment)
    store.retry(run_id)
    tick(store)
    assert store.run(run_id)["status"] == "demo_completed"
    assert store.run(run_id)["confirmation"] is None


@pytest.mark.parametrize("source", ["discovered", "existing_issue"])
def test_failed_review_never_produces_pr_or_fix_memory(store, monkeypatch, source):
    verify_patch = fixture.verify_patch
    monkeypatch.setattr(fixture, "verify_patch", lambda: {"passed": 0, "failed": 1})
    if source == "discovered":
        run_id = store.start_run("repo_fixture")
        tick(store)
        store.observe(run_id, 20, 3, 10)
        store.confirm(run_id, "confirmed", fixture.REVISION, "")
    else:
        run_id = store.start_issue_sample()
    tick(store)
    run = store.details(run_id)
    assert run["status"] == "failed"
    assert "pr_draft" not in {a["kind"] for a in run["artifacts"]}
    assert not Memory(store).list("repo_fixture", category="fix")["items"]
    with pytest.raises(Conflict):
        store.complete(run_id, "pretend completed")
    monkeypatch.setattr(fixture, "verify_patch", verify_patch)
    store.retry(run_id)
    tick(store)
    assert store.run(run_id)["status"] == "demo_completed"
    assert len(Memory(store).list("repo_fixture", category="fix")["items"]) == 1


@pytest.mark.parametrize("field,value", [
    ("run_id", "different-run"), ("repo_id", "different-repo"),
    ("revision", "0" * 64), ("issue_ref", "fixture://different/issue"),
    ("actor", "owner"), ("decision", "failed"),
])
def test_automatic_evidence_is_bound_to_current_task(store, field, value):
    run_id = store.start_issue_sample()
    store.set_running(run_id)
    evidence = store.validate_issue_sample(run_id)
    evidence[field] = value
    with store.connection() as db:
        db.execute("UPDATE runs SET automatic_validation=? WHERE id=?",
                   (json.dumps(evidence), run_id))
    with pytest.raises(Conflict):
        store.artifact(run_id, "patch_draft", {"diff": fixture.PATCH})


def test_forged_source_rejected_at_api_and_origin_is_immutable(store):
    app = create_app(store.path, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/samples/existing-issue/runs").status_code == 401
        session = client.post("/api/setup", json={"password": "policy-test-password"}).json()
        headers = {"X-CSRF-Token": session["csrf"]}
        for endpoint in ("/api/repositories/repo_fixture/runs",
                         "/api/samples/existing-issue/runs"):
            assert client.post(endpoint).status_code == 403
            assert client.post(endpoint, headers=headers,
                               json={"source_kind": "existing_issue"}).status_code == 422
        run_id = client.post("/api/samples/existing-issue/runs",
                             headers=headers, json={}).json()["run_id"]
        assert store.run(run_id)["source_kind"] == "existing_issue"
        config = client.get("/api/system").json()
        assert config["existing_issue_intake"] and not config["github_publication"]
    discovered = store.start_run("repo_fixture")
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("UPDATE runs SET source_kind='existing_issue' WHERE id=?", (discovered,))
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("UPDATE runs SET issue_ref=? WHERE id=?", (FIXTURE_ISSUE_REF, discovered))


def test_additive_migration_preserves_legacy_pending_snapshot(store):
    run_id = store.start_run("repo_fixture")
    # Produce a pre-change unconditional SDK approval checkpoint.
    workflow = Workflow(store)
    workflow.agents["reproduction_assistant"].tools[0].needs_approval = True
    asyncio.run(workflow.process(run_id, "start"))
    snapshot = store.run(run_id, private=True)["snapshot"]
    with store.connection() as db:
        db.execute("DROP TRIGGER immutable_run_origin")
        for name in ("source_kind", "issue_ref", "automatic_validation"):
            db.execute(f"ALTER TABLE runs DROP COLUMN {name}")
        db.execute("UPDATE jobs SET status='done' WHERE run_id=?", (run_id,))
    migrated = Store(store.path)
    run = migrated.run(run_id, private=True)
    assert run["source_kind"] == "discovered"
    assert run["snapshot"] == snapshot
    assert run["automatic_validation"] is None
    migrated.observe(run_id, 20, 3, 10)
    migrated.confirm(run_id, "confirmed", fixture.REVISION, "")
    tick(migrated)
    assert migrated.run(run_id)["status"] == "demo_completed"


def test_no_artifacts_or_completion_before_automatic_validation(store):
    run_id = store.start_issue_sample()
    for kind in ("issue_draft", "patch_draft", "verification", "pr_draft"):
        with pytest.raises(Conflict):
            store.artifact(run_id, kind, {})
    with pytest.raises(Conflict):
        store.complete(run_id, "not validated")
    store.set_running(run_id)
    store.validate_issue_sample(run_id)
    with pytest.raises(Conflict):
        store.artifact(run_id, "pr_draft", {"published": False, "merge_enabled": False})
    with pytest.raises(Conflict):
        store.artifact(run_id, "pr_draft", {"published": True})
    with pytest.raises(Conflict):
        store.artifact(run_id, "pr_draft", {"merge_enabled": True})
