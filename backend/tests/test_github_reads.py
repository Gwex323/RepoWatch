import httpx
import pytest

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.github_ports import GitHubReadError
from repowatch.application.github_reads import GitHubReads, MemorySourceFacts
from repowatch.application.storage import ConflictError
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate


def issue(identifier=1, *, date="2026-11-04T00:00:00Z", title="fixture issue", pr=False):
    result = {
        "id": identifier,
        "number": identifier,
        "title": title,
        "body": "fixture source",
        "state": "open",
        "labels": [{"name": "bug"}],
        "assignees": [],
        "updated_at": date,
    }
    if pr:
        result["pull_request"] = {"url": "https://fixture.example/pulls/2"}
    return result


class Wire:
    def __init__(self):
        self.calls = []
        self.items = [issue(), issue(2, pr=True)]
        self.status = 200
        self.conditional = True

    def __call__(self, request):
        assert request.method == "GET"
        assert not request.content
        self.calls.append((request.url.path, request.headers.get("if-none-match")))
        if request.url.path.endswith("/issues"):
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "private test error"})
            if self.conditional and request.headers.get("if-none-match"):
                return httpx.Response(304)
            return httpx.Response(
                200,
                json=self.items,
                headers={
                    "etag": '"fixture-etag"',
                    "link": '<https://api.github.test/repos/o/r/issues?page=2>; rel="next"',
                },
            )
        return httpx.Response(
            200, json={"id": 123, "owner": {"login": "o"}, "name": "r", "default_branch": "main"}
        )


@pytest.fixture
def reader(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    wire = Wire()
    adapter = GitHubReadAdapter(
        base_url="https://api.github.test", transport=httpx.MockTransport(wire)
    )
    service = GitHubReads(store, adapter)
    repo = service.register_identity("o", "r")
    yield store, wire, service, repo
    store.close()


def test_read_only_source_persists_signal_splits_pr_and_preserves_pause(reader):
    store, wire, service, repo = reader
    result = service.read_page("read", repo, now=0)
    assert result["state"] == "completed"
    assert result["result"]["has_next"] and not result["result"]["repository_snapshot_complete"]
    with store.read() as records:
        assert len(records.find("tasks")) == 1
        assert len(records.find("pending_signals")) == 1
        assert len(records.find("github_observations")) == 2
        assert records.find("runs") == []
        assert records.one("global_control", singleton=1)["paused"] == 1
    facts = MemorySourceFacts(store).repository_facts(repo)
    assert len(facts) == 2 and all(row["visibility"] == "repository" for row in facts)
    before = len(wire.calls)
    assert service.read_page("read", repo, now=5) == result
    assert len(wire.calls) == before
    assert service.read_page("conditional", repo, now=5)["state"] == "completed"
    with store.read() as records:
        assert len(records.find("pending_signals")) == 1


def test_bad_or_failed_page_does_not_advance_last_success(reader):
    store, wire, service, repo = reader
    service.read_page("first", repo, now=10)
    wire.status = 500
    result = service.read_page("failure", repo, now=20)
    assert result["state"] == "failed"
    with store.read() as records:
        assert records.one("github_read_cache", repository_id=repo, page=1)["last_success_at"] == 10
    wire.status = 200
    wire.conditional = False
    wire.items = [{"id": 1}]
    assert service.read_page("invalid", repo, now=30)["state"] == "failed"
    with store.read() as records:
        assert records.one("github_read_cache", repository_id=repo, page=1)["last_success_at"] == 10


def test_invalid_304_cache_is_read_unconditionally_and_signal_remains_unique(reader):
    store, wire, service, repo = reader
    service.read_page("first", repo, now=0)
    with store.write() as records:
        records.change(
            "github_read_cache",
            {"repository_id": repo, "page": 1},
            {"observation_ids_json": '["missing"]'},
        )
    result = service.read_page("repair-cache", repo, now=10)
    assert result["state"] == "completed"
    assert wire.calls[-1][1] is None
    with store.read() as records:
        assert len(records.find("pending_signals")) == 1


def test_changed_source_does_not_reenable_user_excluded_task_and_old_source_is_uncertain(reader):
    store, wire, service, repo = reader
    service.read_page("first", repo, now=0)
    with store.read() as records:
        task = dict(records.find("tasks")[0])
    service.tasks.operate(
        "exclude", task["id"], expected_revision=task["revision"], action="exclude", now=0
    )
    wire.conditional = False
    wire.items = [issue(date="2026-11-04T00:01:00Z", title="changed")]
    assert service.read_page("changed", repo, now=60)["state"] == "completed"
    with store.read() as records:
        task = records.one("tasks", id=task["id"])
        assert task["user_excluded"] == 1 and task["state"] == "ended"
        assert len(records.find("pending_signals")) == 2
    wire.items = [issue()]
    assert service.read_page("old", repo, now=120)["error"] == "source_order_uncertain"
    with store.read() as records:
        assert records.one("github_read_cache", repository_id=repo, page=1)["last_success_at"] == 60


def test_memory_sources_are_repository_scoped_and_immutable(reader):
    store, _, service, repo = reader
    service.read_page("read", repo, now=0)
    other = service.tasks.register_repository("other", admission_mode="manual")
    assert MemorySourceFacts(store).repository_facts(other) == []
    with store.read() as records:
        observation = dict(records.find("github_observations")[0])
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.change("github_observations", {"id": observation["id"]}, {"payload_json": "{}"})
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.add("github_observations", {**observation, "id": "unlinked", "object_id": "99"})
            records.add(
                "memory_source_facts",
                {"observation_id": "unlinked", "repository_id": other, "visibility": "repository"},
            )


def test_adapter_refuses_redirects_invalid_identity_and_write_paths():
    calls = []

    def redirect(request):
        calls.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://other.example/private"})

    adapter = GitHubReadAdapter(transport=httpx.MockTransport(redirect))
    with pytest.raises(GitHubReadError):
        adapter.repository("o", "r")
    assert calls == ["api.github.com"]
    with pytest.raises(ValueError):
        adapter.repository("../other", "r")
