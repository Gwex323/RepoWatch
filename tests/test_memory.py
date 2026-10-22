import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from repowatch.app import create_app
from repowatch.memory import Memory
from repowatch.store import Conflict, Missing, Store


def test_owner_memory_scope_versions_and_archives(tmp_path):
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    repo = store.add_repository("owner/project", "https://github.com/owner/project")["id"]
    other = store.add_repository("other/project", "https://github.com/other/project")["id"]
    values = dict(category="testing", title="Exact 100%_ reference", content="pytest", revision="a")
    note = memory.create(repo, **values)
    memory.create(other, **values)
    memory.create(repo, **(values | {"revision": "b"}))
    assert memory.list(repo, revision="a")["total"] == 1
    assert memory.list(repo, query="100%_")["total"] == 2
    assert memory.list(repo, query="' OR 1=1 --")["total"] == 0
    with pytest.raises(Missing):
        memory.history(other, note["id"])
    updated = memory.update(repo, note["id"], version=1, **(values | {"content": "pytest -q"}))
    assert updated["version"] == 2
    with pytest.raises(Conflict):
        memory.archive(repo, note["id"], version=1)
    memory.archive(repo, note["id"], version=2)
    assert memory.list(repo, status="archived")["total"] == 1
    assert memory.restore(repo, note["id"], version=3)["version"] == 4
    assert [item["action"] for item in memory.history(repo, note["id"])] == [
        "restored", "archived", "edited", "created",
    ]
    assert Memory(Store(store.path)).list(repo, revision="a")["items"][0]["content"] == "pytest -q"


def test_rank_prefers_relevant_paths_and_issue_text():
    relevant = SimpleNamespace(
        id="relevant", category="fix", title="Fixture suspected behavior",
        content="Reference for app.py", updated=time.time(),
        evidence=json.dumps({"experience": {
            "problem": {"title": "Fixture suspected behavior", "summary": "fixture"},
            "change": {"summary": "Fix app.py", "paths": ["app.py"],
                       "regression_paths": []},
        }}),
    )
    unrelated = SimpleNamespace(
        id="unrelated", category="fix", title="Unrelated", content="Other topic",
        updated=time.time(), evidence="{}",
    )
    ranked = Memory.rank(
        [unrelated, relevant],
        {"files": {"app.py": "print('ok')"},
         "issue": {"title": "Fixture suspected behavior", "body": "app.py"}},
    )
    assert [row.id for row in ranked] == ["relevant", "unrelated"]


def test_memory_api_validation_auth_isolation_and_pagination(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        repo = app.state.store.add_repository('owner/project', 'https://github.com/owner/project')
        base = f"/api/repositories/{repo['id']}/memories"
        assert client.get(base).status_code == 401
        result = client.post("/api/setup", json={"password": "local-test-password"})
        headers = {"X-CSRF-Token": result.json()["csrf"]}
        body = {"category": "testing", "title": "Tests", "content": "pytest", "revision": "abc"}
        assert client.post(base, json=body).status_code == 403
        assert client.post(
            base, json=body, headers=headers | {"Origin": "https://other.example"}
        ).status_code == 403
        for forged in ({"category": "bug"}, {"source": "workflow"}, {"source_run_id": "another"},
                       {"evidence": {"verified": True}}, {"content": "a" * 4001}):
            assert client.post(base, json=body | forged, headers=headers).status_code == 422
        assert client.post(base, json=body | {"title": "  "}, headers=headers).status_code == 409
        item = client.post(base, json=body, headers=headers).json()
        assert item["source"] == "owner"
        assert client.get(base + "?limit=0").status_code == 422
        assert client.get(base + "?category=unknown").status_code == 422
        assert client.get(base + "?revision=abc").json()["total"] == 1
        assert client.get(base + "?revision=def").json()["total"] == 0
        assert client.get(base + "?offset=1").json()["items"] == []
        path = f"{base}/{item['id']}"
        assert client.post(
            path, json=body | {"version": 1, "content": "pytest -q"}, headers=headers
        ).json()["version"] == 2
        assert client.post(
            path, json=body | {"version": 1}, headers=headers
        ).status_code == 409
        other = client.post(
            "/api/repositories", json={"url": "https://github.com/other/repo"}, headers=headers
        ).json()["id"]
        assert client.get(
            f"/api/repositories/{other}/memories/{item['id']}/history"
        ).status_code == 404
        assert client.get(path + "/history").json()[0]["record"]["content"] == "pytest -q"
        assert client.post(
            path + "/archive", json={"version": 2}, headers=headers
        ).status_code == 200
        assert client.get(base).json()["total"] == 0
        assert client.get(base + "?status=archived").json()["total"] == 1
        assert client.post(
            path + "/restore", json={"version": 3}, headers=headers
        ).json()["version"] == 4
        assert client.get(base).json()["total"] == 1
        assert client.get(path + "/history").json()[0]["action"] == "restored"
        metrics = f"/api/repositories/{repo['id']}/memory-metrics"
        empty = client.get(metrics, headers=headers).json()
        assert empty["metric_version"] == "structured_effect_v1"
        assert empty["with_memory"]["analyses"] == 0
        assert empty["without_memory"]["analyses"] == 0
        assert client.get(
            metrics + "?revision=abc", headers=headers
        ).json()["with_memory"]["analyses"] == 0
        aging = client.get(
            f"/api/repositories/{repo['id']}/memory-aging?days=1", headers=headers
        ).json()
        assert aging["threshold_days"] == 1
        assert aging["items"][0]["title"] == "Tests"
        assert "stale" in aging["items"][0]
