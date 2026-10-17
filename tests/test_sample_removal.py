"""Explicit fixture cleanup never removes real repository data or personal notes."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from repowatch import fixture
from repowatch.app import create_app
from repowatch.memory import Memory
from repowatch.store import Conflict, Missing, Store
from repowatch.workflow import Worker


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "samples.db")


def note(store, repo_id="repo_fixture"):
    return Memory(store).create(repo_id, category="testing", title="Personal note",
                                content="Keep owner-authored knowledge.", revision=fixture.REVISION)


def test_removal_clears_fixture_graph_and_survives_restart(store):
    real = store.add_repository("samples/page-window", "https://github.com/samples/page-window")
    personal = note(store, real["id"])
    sample = store.start_run("repo_fixture")
    assert asyncio.run(Worker(store).tick())
    store.observe(sample, 20, 3, 10)
    store.confirm(sample, "confirmed", fixture.REVISION, "")
    assert asyncio.run(Worker(store).tick())
    assert store.run(sample)["status"] == "demo_completed"
    with store.connection() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE repo_id='repo_fixture'",
        ).fetchone()[0]
        assert count > 0
        before = dict(db.execute("SELECT * FROM memories WHERE id=?", (personal["id"],)).fetchone())
    removed = store.remove_samples()
    assert removed["repositories"] == 1 and removed["runs"] == 1
    assert removed["memories"] > 0 and removed["artifacts"] > 0 and removed["events"] > 0
    with store.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        after = dict(db.execute("SELECT * FROM memories WHERE id=?", (personal["id"],)).fetchone())
        assert after == before
        assert db.execute("SELECT COUNT(*) FROM memory_history WHERE memory_id=?",
                          (personal["id"],)).fetchone()[0] == 1
        for table in ("runs", "jobs", "artifacts", "events", "memory_reads"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    restarted = Store(store.path)
    assert restarted.setting("samples_enabled") == "false"
    assert [r["id"] for r in restarted.repositories()] == [real["id"]]
    assert restarted.dashboard()["runs"] == []
    assert restarted.dashboard()["events"] == []
    with pytest.raises(Missing):
        restarted.run(sample)
    with pytest.raises(Conflict, match="samples_disabled"):
        restarted.start_run("repo_fixture")
    with pytest.raises(Conflict, match="samples_disabled"):
        restarted.start_issue_sample()
    assert all(count == 0 for count in restarted.remove_samples().values())


def test_pending_sample_is_removed_without_executing_its_job(store):
    sample = store.start_run("repo_fixture")
    assert store.remove_samples()["jobs"] == 1
    assert not asyncio.run(Worker(store).tick())
    with pytest.raises(Missing):
        store.run(sample)


@pytest.mark.parametrize("kind", [
    "owner_memory", "running_job", "execution_data", "external_reference",
])
def test_protected_data_blocks_cleanup_atomically(store, kind):
    sample = store.start_run("repo_fixture")
    expected = {
        "owner_memory": "sample_has_owner_memory", "running_job": "sample_busy",
        "execution_data": "sample_has_execution_data",
        "external_reference": "sample_has_external_references",
    }[kind]
    if kind == "owner_memory":
        note(store)
    elif kind == "running_job":
        with store.connection() as db:
            db.execute("UPDATE jobs SET status='running' WHERE run_id=?", (sample,))
    elif kind == "execution_data":
        with store.connection() as db:
            db.execute("INSERT INTO environments(id,repo_id,account_id,account_version,"
                       "state,created,updated) "
                       "VALUES('env_protected','repo_fixture',1,1,'stopped',1,1)")
    else:
        real = store.add_repository("owner/real", "https://github.com/owner/real")
        personal = note(store, real["id"])
        with store.connection() as db:
            db.execute("UPDATE memories SET source_run_id=? WHERE id=?", (sample, personal["id"]))
    with pytest.raises(Conflict, match=expected):
        store.remove_samples()
    assert store.setting("samples_enabled") is None
    assert store.run(sample)["id"] == sample
    assert any(r["kind"] == "fixture" for r in store.repositories())
    with store.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_removal_does_not_depend_on_known_sample_name_or_id(store):
    with store.connection() as db:
        db.execute("INSERT INTO repositories(id,name,kind,language,created) "
                   "VALUES('other_fixture','Another fixture','fixture','Python',1)")
    real = store.add_repository("owner/real", "https://github.com/owner/real")
    assert store.remove_samples()["repositories"] == 2
    assert [r["id"] for r in store.repositories()] == [real["id"]]


def test_sample_api_cannot_recreate_removed_fixture(tmp_path):
    path = tmp_path / "api.db"
    app = create_app(path, start_worker=False)
    app.state.store.setup_password("test-password-only")
    app.state.store.remove_samples()
    with TestClient(create_app(path, start_worker=False), base_url="http://127.0.0.1") as client:
        login = client.post("/api/login", json={"password": "test-password-only"})
        assert login.status_code == 200
        headers = {"x-csrf-token": login.json()["csrf"]}
        assert client.get("/api/dashboard").json() == {"repositories": [], "runs": [], "events": []}
        for route in ("/api/repositories/repo_fixture/runs", "/api/samples/existing-issue/runs"):
            response = client.post(route, json={}, headers=headers)
            assert response.status_code == 409 and response.json()["detail"] == "samples_disabled"
        assert client.get("/api/dashboard").json()["repositories"] == []
