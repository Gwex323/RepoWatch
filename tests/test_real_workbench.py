"""The product exposes only real repository flows and migrates old data safely."""

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from test_execution import service  # noqa: F401
from test_repository_analysis import Engine, prepare_tested

from repowatch.app import create_app
from repowatch.memory import Memory, insert
from repowatch.repository_analysis import ROLES as ANALYSIS_ROLES
from repowatch.repository_analysis import Analyses
from repowatch.roles import ROLES
from repowatch.store import Store


def test_fresh_workspace_has_only_real_roles_and_no_sample_endpoints(tmp_path):
    app = create_app(tmp_path / "app.db", start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/dashboard").status_code == 401
        login = client.post("/api/setup", json={"password": "owner-test-password"})
        headers = {"X-CSRF-Token": login.json()["csrf"]}
        assert client.get("/api/dashboard").json() == {
            "repositories": [], "findings": [], "events": [],
            "counts": {"waiting": 0, "active": 0, "published_prs": 0},
        }
        system = client.get("/api/system").json()
        assert system["roles"] == list(ROLES)
        assert set(ROLES) == set(ANALYSIS_ROLES) | {"repair_agent"}
        assert system["model_mode"] == "configured_provider"
        for path in ("/api/repositories/repo_fixture/runs", "/api/samples/existing-issue/runs",
                     "/api/runs/old/experiment", "/api/runs/old/confirmation",
                     "/api/runs/old/retry"):
            assert client.post(path, json={}, headers=headers).status_code == 404
        assert client.get("/api/runs/old").status_code == 404
        assert client.get("/api/runs/old/memories").status_code == 404
    with app.state.store.connection() as db:
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        assert not tables & {"runs", "jobs", "events", "artifacts", "memory_reads"}


def test_authentication_and_repository_validation_survive_removal(tmp_path):
    app = create_app(tmp_path / "app.db", start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login = client.post("/api/setup", json={"password": "owner-test-password"})
        headers = {"X-CSRF-Token": login.json()["csrf"]}
        body = {"url": "https://github.com/owner/project"}
        assert client.post("/api/repositories", json=body).status_code == 403
        assert client.post("/api/repositories", json=body, headers={
            **headers, "Origin": "https://attacker.example",
        }).status_code == 403
        for url in ("file:///etc/passwd", "https://github.com/owner/project/tree/main",
                    "https://evil.example/owner/project"):
            assert client.post("/api/repositories", json={"url": url},
                               headers=headers).status_code == 422
        response = client.post("/api/repositories", json=body, headers=headers)
        assert response.status_code == 201
        assert client.post(
            "/api/repositories", json=body, headers=headers,
        ).json() == response.json()
        assert client.get("/api/dashboard").headers["cache-control"] == "no-store"
        assert client.post("/api/setup", json={"password": "another-password"}).status_code == 409
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert client.get("/api/dashboard").status_code == 401
        assert client.post("/api/login", json={"password": "incorrect-password"}).status_code == 401
        assert client.post(
            "/api/login", json={"password": "owner-test-password"},
        ).status_code == 200


def test_dashboard_uses_real_findings_activity_and_memory_reads(service):  # noqa: F811
    execution, repo, _ = service
    env = prepare_tested(service)
    note = Memory(execution.store).create(
        repo["id"], category="testing", title="Project tests", content="Review app.py",
        revision=env["revision"],
    )
    analyses = Analyses(execution, Engine())
    assert asyncio.run(analyses.tick())
    result = analyses.list(env["id"])[0]
    dashboard = execution.store.dashboard()
    assert len(dashboard["findings"]) == 1
    finding = dashboard["findings"][0]
    assert finding["id"] == result["findings"][0]["id"]
    assert finding["title"] == result["findings"][0]["data"]["title"]
    assert finding["repo_id"] == repo["id"] and finding["env_id"] == env["id"]
    assert dashboard["counts"] == {"waiting": 1, "active": 0, "published_prs": 0}
    assert {event["source"] for event in dashboard["events"]} == {"analysis", "execution"}
    assert len({event["id"] for event in dashboard["events"]}) == len(dashboard["events"])
    aging = Memory(execution.store).aging(repo["id"])
    assert aging["items"][0]["id"] == note["id"]
    assert aging["items"][0]["read_count"] == 1
    with execution.store.connection() as db:
        db.execute(
            "INSERT INTO repair_tasks(id,analysis_id,finding_id,env_id,repo_id,revision,"
            "status,context,created,updated) VALUES(?,?,?,?,?,?,'pr_created','{}',1,1)",
            ("repair_overview", result["id"], finding["id"], env["id"],
             repo["id"], env["revision"]),
        )
        db.execute("INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,?)",
                   ("repair_overview", "publication", "x" * 1000, time.time()))
        db.execute("UPDATE repository_analyses SET created=? WHERE id=?",
                   (time.time() - 100 * 86400, result["id"]))
        db.execute("UPDATE memories SET updated=? WHERE id=?",
                   (time.time() - 100 * 86400, note["id"]))
    assert Memory(execution.store).aging(repo["id"], days=30)["items"][0]["stale"]
    dashboard = execution.store.dashboard()
    assert dashboard["counts"]["published_prs"] == 1
    assert {event["source"] for event in dashboard["events"]} == {
        "analysis", "execution", "repair",
    }
    assert {event["env_id"] for event in dashboard["events"]} == {env["id"]}
    assert all(len(event["detail"]) <= 500 for event in dashboard["events"])


def legacy_database(path, *, personal=False):
    """Seed the discontinued schema without retaining its executable implementation."""
    store = Store(path)
    real = store.add_repository("owner/real", "https://github.com/owner/real")
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE runs(id TEXT PRIMARY KEY, repo_id TEXT REFERENCES repositories(id));
            CREATE TABLE jobs(id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),status TEXT);
            CREATE TABLE events(id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id));
            CREATE TABLE artifacts(run_id TEXT REFERENCES runs(id),kind TEXT);
            CREATE TABLE memory_reads(run_id TEXT REFERENCES runs(id),memory_id TEXT);
            ALTER TABLE memories ADD COLUMN source_run_id TEXT REFERENCES runs(id);
            INSERT INTO repositories VALUES(
                'old_demo','old pagination',NULL,'fixture','Python',0,1
            );
            INSERT INTO runs VALUES('old_run','old_demo');
            INSERT INTO jobs VALUES('old_job','old_run','running');
            INSERT INTO events VALUES('old_event','old_run');
            INSERT INTO artifacts VALUES('old_run','patch_draft');
            INSERT INTO settings VALUES('sentinel','keep');
            INSERT INTO settings VALUES('samples_enabled','true');
        """)
        generated = insert(db, repo_id="old_demo", category="fix", title="Old generated patch",
                           content="discard", revision="old", source="workflow",
                           source_key="run:old_run:fix")
        db.execute("INSERT INTO memory_reads VALUES('old_run',?)", (generated["id"],))
    owner = Memory(store).create(
        "old_demo" if personal else real["id"], category="testing", title="Personal knowledge",
        content="Keep this exact content", revision="old",
    )
    with store.connection() as db:
        # Also keep provenance on real memory that referenced an old sample run.
        db.execute("UPDATE memories SET source_run_id='old_run' WHERE id=?", (owner["id"],))
    return real, owner


@pytest.mark.parametrize("personal", [False, True])
def test_upgrade_retires_old_jobs_atomically_and_preserves_notes(tmp_path, personal):
    path = tmp_path / "upgrade.db"
    real, owner = legacy_database(path, personal=personal)
    store = Store(path)
    assert [repo["id"] for repo in store.repositories()] == [real["id"]]
    assert store.dashboard()["findings"] == [] and store.dashboard()["events"] == []
    assert store.setting("sentinel") == "keep" and store.setting("samples_enabled") is None
    with store.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        saved = db.execute("SELECT * FROM memories WHERE id=?", (owner["id"],)).fetchone()
        assert saved["content"] == owner["content"] and saved["source_run_id"] == "old_run"
        assert json.loads(db.execute("SELECT record FROM memory_history WHERE memory_id=?",
                                     (owner["id"],)).fetchone()[0])["content"] == owner["content"]
        assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        if personal:
            assert db.execute(
                "SELECT kind FROM repositories WHERE id='old_demo'",
            ).fetchone()[0] == "archived"
        else:
            assert not db.execute("SELECT 1 FROM repositories WHERE id='old_demo'").fetchone()
    assert Store(path).dashboard() == store.dashboard()


def test_upgrade_keeps_existing_real_analyses_and_execution_records(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    assert asyncio.run(analyses.tick())
    tables = (
        "environments", "execution_jobs", "repository_analyses", "analysis_findings",
        "analysis_events", "repair_tasks", "repair_events", "github_accounts", "analysis_model",
    )
    with execution.store.connection() as db:
        before = {name: [dict(row) for row in db.execute(f"SELECT * FROM {name}")]
                  for name in tables}
    legacy_database(execution.store.path)
    migrated = Store(execution.store.path)
    with migrated.connection() as db:
        for table in tables:
            assert [dict(row) for row in db.execute(f"SELECT * FROM {table}")] == before[table]
    assert migrated.dashboard()["findings"][0]["env_id"] == env["id"]


def test_upgrade_failure_rolls_back_schema_and_rows(tmp_path):
    path = tmp_path / "rollback.db"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
        db.execute("CREATE TRIGGER block_retirement BEFORE DELETE ON repositories "
                   "WHEN OLD.kind='fixture' BEGIN SELECT RAISE(ABORT,'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "running"
        assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 2
        db.execute("DROP TRIGGER block_retirement")
        assert list(db.iterdump()) == before
