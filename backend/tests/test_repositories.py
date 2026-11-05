import json
from types import SimpleNamespace

import httpx
import pytest

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.github_ports import GitHubReadError
from repowatch.application.repositories import RepositoryService
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.repositories import RepositoryRequest
from repowatch.operations.migrate import migrate


@pytest.fixture
def repositories(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    wire = SimpleNamespace(
        private=False,
        maintained=True,
        fork_parent=10,
        requests=[],
        status=200,
        branch_status=200,
        fork_push=True,
    )

    def handle(request):
        wire.requests.append(request)
        assert request.method == "GET"
        if wire.status != 200:
            return httpx.Response(wire.status)
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "me"})
        if "/branches/" in path:
            if wire.branch_status != 200:
                return httpx.Response(wire.branch_status)
            return httpx.Response(200, json={"commit": {"sha": "a" * 40}})
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        fork = path == "/repos/me/fork"
        return httpx.Response(
            200,
            json={
                "id": 20 if fork else 10,
                "owner": {"login": "me" if fork else "up"},
                "name": "fork" if fork else "repo",
                "private": wire.private,
                "permissions": {"push": wire.fork_push if fork else wire.maintained},
                "default_branch": "main",
                "fork": fork,
                "parent": {"id": wire.fork_parent},
                "has_issues": True,
            },
        )

    adapter = GitHubReadAdapter(transport=httpx.MockTransport(handle))
    yield store, wire, RepositoryService(store, lambda _: adapter)
    store.close()


def register(service, operation, *, now=0, revision=0, policy=None, **request):
    preflight = service.preflight(
        operation + "-check",
        {"owner": "up", "name": "repo", "credential_reference": "github", **request},
        now=now,
    )
    return service.save(
        operation, preflight["preflight_id"], policy or {}, expected_revision=revision, now=now
    )


@pytest.mark.parametrize("private", [False, True])
def test_direct_public_private_default_policy_and_atomic_import(repositories, private):
    store, wire, service = repositories
    wire.private = private
    result = register(service, "first")
    assert result["policy"]["mode"] == "all_bugs"
    assert result["policy"]["claim_comment"] is False
    with store.read() as records:
        repository = records.one("repositories", id=result["repository_id"])
        assert repository["scan_ready_sha"] is None
        assert len(records.find("repository_imports")) == 1
        assert records.one("tasks", id=result["scan_task_id"])["mode"] == "scan"
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_fork_path_separate_identity_default_labels_and_relation_refusal(repositories):
    store, wire, service = repositories
    wire.maintained = False
    result = register(service, "fork", contribution_path="fork", fork_owner="me", fork_name="fork")
    assert result["policy"]["mode"] == "labels"
    assert result["admission_status"] == "configuration_required"
    with store.read() as records:
        repository = records.one("repositories", id=result["repository_id"])
        assert json.loads(repository["publication_json"])["github_id"] == "20"
    wire.fork_parent = 99
    with pytest.raises(GitHubReadError):
        register(service, "wrong", contribution_path="fork", fork_owner="me", fork_name="fork")
    with pytest.raises(GitHubReadError):
        register(service, "direct")


def test_fork_path_requires_configured_admission_labels(repositories):
    store, wire, service = repositories
    wire.maintained = False
    result = register(
        service, "fork-no-labels", contribution_path="fork", fork_owner="me", fork_name="fork"
    )
    assert result["admission_status"] == "configuration_required"
    with store.read() as records:
        repository = records.one("repositories", id=result["repository_id"])
        assert repository["admission_mode"] == "labels"
        assert json.loads(repository["policy_json"])["allowed_labels"] == []


def test_reimport_idempotency_and_target_snapshots(repositories):
    store, wire, service = repositories
    first = register(service, "first")
    tasks = TaskRecords(store)
    issue = tasks.ensure_task(first["repository_id"], "issue:7", issue_id="7", now=0)
    with store.write() as records:
        records.change("repositories", {"id": first["repository_id"]}, {"scan_ready_sha": "a" * 40})
        records.change(
            "tasks",
            {"id": issue},
            {"mode": "implement", "configuration_json": '{"target_branch":"main"}'},
        )
    preflight = service.preflight(
        "check-second",
        {
            "owner": "up",
            "name": "repo",
            "target_branch": "release/v2",
            "credential_reference": "github",
        },
        now=1,
    )
    second = service.save(
        "second", preflight["preflight_id"], {"mode": "manual"}, expected_revision=1, now=1
    )
    assert (
        service.save(
            "second", preflight["preflight_id"], {"mode": "manual"}, expected_revision=1, now=1
        )
        == second
    )
    assert second["import_epoch"] == 2 and second["target_changed"]
    with store.read() as records:
        assert len(records.find("repositories")) == 1
        assert len(records.find("repository_imports")) == 2
        assert records.one("repositories", id=first["repository_id"])["scan_ready_sha"] is None
        preserved = records.one("tasks", id=issue)
        assert preserved["configuration_json"] == '{"target_branch":"main"}'
        assert preserved["wait_reason"] == "target_configuration"
    with pytest.raises(ConflictError):
        service.save("stale", preflight["preflight_id"], {}, expected_revision=1, now=2)
    with pytest.raises(ConflictError):
        service.save("expired", preflight["preflight_id"], {}, expected_revision=2, now=302)
    with pytest.raises(ConflictError):
        service.save(
            "second", preflight["preflight_id"], {"mode": "labels"}, expected_revision=1, now=2
        )


def test_same_sha_reimport_creates_new_scan_and_policy_versions(repositories):
    store, _, service = repositories
    first = register(service, "first")
    second = register(
        service,
        "second",
        revision=1,
        policy={
            "mode": "labels",
            "allowed_labels": ["agent"],
            "severity_labels": {"urgent": 4},
            "claim_comment": True,
        },
    )
    assert first["scan_task_id"] != second["scan_task_id"]
    with store.read() as records:
        repository = records.one("repositories", id=first["repository_id"])
        assert repository["target_revision"] == 1
        assert repository["admission_revision"] == 2
        assert len(records.find("repository_revisions")) == 2


def test_invalid_paths_rejected_without_requests(repositories):
    _, wire, service = repositories
    with pytest.raises(ValueError):
        service.preflight("bad", {"owner": "../private", "name": "repo"}, now=0)
    assert wire.requests == []
    with pytest.raises(ValueError):
        RepositoryRequest(owner="up", name="repo", contribution_path="fork")
    with pytest.raises(ValueError):
        RepositoryRequest(owner="up", name="repo", credential_reference=None)
    with pytest.raises(ValueError):
        RepositoryRequest(
            owner="up", name="repo", target_branch="release/../main", credential_reference="github"
        )


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_permission_and_branch_failures_do_not_import(repositories, status):
    store, wire, service = repositories
    wire.status = status
    with pytest.raises(GitHubReadError):
        register(service, "permission")
    wire.status, wire.branch_status = 200, status
    with pytest.raises(GitHubReadError):
        register(service, "branch")
    with store.read() as records:
        assert records.find("repositories") == []
        assert records.find("tasks") == []


def test_private_fork_and_unwritable_fork_refused(repositories):
    _, wire, service = repositories
    wire.private = True
    with pytest.raises(GitHubReadError):
        register(
            service, "private-fork", contribution_path="fork", fork_owner="me", fork_name="fork"
        )
    wire.private, wire.fork_push = False, False
    with pytest.raises(GitHubReadError):
        register(
            service, "readonly-fork", contribution_path="fork", fork_owner="me", fork_name="fork"
        )


def test_same_target_reimport_replaces_dependencies_without_retargeting(repositories):
    store, _, service = repositories
    first = register(service, "first")
    tasks = TaskRecords(store)
    task_id = tasks.ensure_task(first["repository_id"], "issue:8", issue_id="8", now=0)
    with store.write() as records:
        records.change("tasks", {"id": task_id}, {"mode": "implement"})
    second = register(service, "second", revision=1)
    third = register(service, "third", revision=2)
    with store.read() as records:
        task = records.one("tasks", id=task_id)
        assert task["state"] == "waiting" and task["wait_reason"] == "scan"
        dependencies = records.find("scan_dependencies", waiting_task_id=task_id)
        assert len(dependencies) == 1
        assert dependencies[0]["scan_task_id"] == third["scan_task_id"]
        assert dependencies[0]["scan_task_id"] != second["scan_task_id"]


def test_drift_preserves_budget_and_target_configuration_revision(repositories):
    store, _, service = repositories
    first = register(service, "first")
    scan_id = service.record_target_drift(
        first["repository_id"],
        "b" * 40,
        now=1,
        owner=first["scan_task_id"],
        expected_revision=1,
    )
    with store.read() as records:
        repository = records.one("repositories", id=first["repository_id"])
        assert repository["target_sha"] == "b" * 40
        assert repository["target_revision"] == 1
        assert repository["import_epoch"] == 1
        assert repository["scan_ready_json"] is None
        assert records.one("tasks", id=scan_id)["budget_owner_task_id"] == first["scan_task_id"]
    assert (
        service.record_target_drift(
            first["repository_id"],
            "b" * 40,
            now=2,
            owner=first["scan_task_id"],
            expected_revision=2,
        )
        is None
    )
    with pytest.raises(ConflictError):
        service.record_target_drift(
            first["repository_id"],
            "c" * 40,
            now=3,
            owner=first["scan_task_id"],
            expected_revision=1,
        )


def test_scan_policy_changes_increment_only_scan_policy_revision(repositories):
    store, _, service = repositories
    first = register(service, "first")
    preflight = service.preflight(
        "policy-preflight",
        {
            "owner": "up",
            "name": "repo",
            "credential_reference": "github",
        },
        now=1,
    )
    service.save(
        "scan-policy",
        preflight["preflight_id"],
        {},
        expected_revision=1,
        now=1,
        scan_policy={"chunk_bytes": 256},
    )
    with store.read() as records:
        repository = records.one("repositories", id=first["repository_id"])
        assert repository["scan_policy_revision"] == 2
        assert repository["admission_revision"] == 1
        assert json.loads(repository["scan_policy_json"])["chunk_bytes"] == 256
