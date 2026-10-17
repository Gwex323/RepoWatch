"""Exercise real REST payloads against a fake GitHub server, never a live repository."""

import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from test_execution import service  # noqa: F401
from test_github_accounts import TOKEN_A
from test_repairs import confirm, enable, pending  # noqa: F401

from repowatch.github_accounts import GitHubClient, GitHubError
from repowatch.github_contributions import GitHubPublisher
from repowatch.store import Conflict


def object_sha(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class ContributionRemote:
    def __init__(self, revision):
        self.revision = revision
        self.push = True
        self.private = False
        self.fork = None
        self.unrelated_fork = False
        self.calls, self.branches, self.issues, self.prs = [], {}, [], []
        self.original_blob = hashlib.sha1(b"blob 11\0print('ok')").hexdigest()
        self.lost_pr = False
        self.missing_pr = False
        self.foreign_marker = False
        self.branch_conflict = False
        self.excess_issues = False
        self.tree_truncated = False
        self.client = GitHubClient(httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer " + TOKEN_A
        path, method = request.url.path, request.method
        data = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"id": 101, "login": "alice", "type": "User"})
        if path.casefold() == "/repos/upstream/project" and method == "GET":
            return httpx.Response(200, json={
                "id": 99, "full_name": "upstream/project", "private": self.private,
                "default_branch": "main", "permissions": {"push": self.push}, "allow_forking": True,
            })
        if path == "/repos/alice/project" and method == "GET":
            if self.unrelated_fork:
                return httpx.Response(200, json={"full_name": "alice/project", "fork": False})
            return httpx.Response(200, json=self.fork) if self.fork else httpx.Response(404)
        if path == "/repos/upstream/project/forks" and method == "POST":
            self.fork = {
                "full_name": "alice/project", "fork": True, "permissions": {"push": True},
                "parent": {"full_name": "upstream/project"},
            }
            return httpx.Response(202, json=self.fork)
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": self.revision}})
        if "/git/ref/heads/repowatch/" in path:
            name = path.split("/git/ref/heads/")[1]
            commit = self.branches.get(name)
            if self.branch_conflict and commit:
                commit = "d" * 40
            return httpx.Response(200, json={"object": {"sha": commit}}) if commit else (
                httpx.Response(404)
            )
        if method == "GET" and "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
        if method == "GET" and "/git/trees/" in path:
            return httpx.Response(200, json={
                "truncated": self.tree_truncated,
                "tree": [{"path": "app.py", "mode": "100755", "type": "blob",
                          "sha": self.original_blob}],
            })
        if path.endswith("/git/blobs") and method == "POST":
            content = base64.b64decode(data["content"])
            return httpx.Response(201, json={
                "sha": hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content
                ).hexdigest(),
            })
        if path.endswith("/git/trees") and method == "POST":
            assert data["base_tree"] == "b" * 40
            assert data["tree"][0]["mode"] == "100755"
            return httpx.Response(201, json={"sha": object_sha(data)})
        if path.endswith("/git/commits") and method == "POST":
            assert data["parents"] == ["a" * 40]
            return httpx.Response(201, json={"sha": object_sha(data)})
        if path.endswith("/git/refs") and method == "POST":
            assert data["ref"] != "refs/heads/main"
            name = data["ref"].removeprefix("refs/heads/")
            self.branches[name] = data["sha"]
            return httpx.Response(201, json={"object": {"sha": data["sha"]}})
        if path.endswith("/issues"):
            if method == "GET":
                return httpx.Response(200, json=[{"body": ""}] * 100 if self.excess_issues
                                      else self.issues)
            item = {**data, "number": 1, "html_url": "https://github.com/upstream/project/issues/1",
                    "user": {"id": 999 if self.foreign_marker else 101}}
            self.issues.append(item)
            return httpx.Response(201, json=item)
        if path.endswith("/pulls"):
            head = ("upstream" if self.push else "alice") + ":repowatch/"
            if method == "GET":
                assert request.url.params["head"].startswith(head)
                return httpx.Response(200, json=self.prs)
            assert data["head"].startswith(head)
            commit = list(self.branches.values())[-1]
            item = {**data, "number": 2, "html_url": "https://github.com/upstream/project/pull/2",
                    "user": {"id": 101}, "head": {"sha": commit}, "base": {"ref": "main"}}
            if not self.missing_pr:
                self.prs.append(item)
            if self.lost_pr or self.missing_pr:
                self.lost_pr = False
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(201, json=item)
        raise AssertionError(f"Unexpected API call: {method} {path}")

    def writes(self, suffix):
        return [request for request in self.calls if request.method == "POST"
                and request.url.path.endswith(suffix)]


@pytest.fixture
def publication(pending):  # noqa: F811
    repairs, _, report, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    publisher.fail = True
    assert asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["review"]["approved"]
    with repairs.store.connection() as db:
        db.execute("UPDATE repositories SET name=?,url=? WHERE id=?",
                   ("upstream/project", "https://github.com/upstream/project", report["repo_id"]))
        row = dict(db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone())
    context = json.loads(row["context"])
    row.update(account_id=context["account_id"], account_version=context["account_version"])
    remote = ContributionRemote(row["revision"])
    repairs.execution.github.client = remote.client
    return GitHubPublisher(repairs.execution.github), row, json.loads(row["patch"]), remote


def test_rest_publication_uses_exact_parent_and_preserves_mode_and_deduplicates(publication):
    publisher, task, patch, remote = publication
    first = publisher.publish(task, patch, "Synthetic reviewed patch", check=lambda: None)
    second = publisher.publish(task, patch, "Synthetic reviewed patch", check=lambda: None)
    assert first == second and first["pr"]["number"] == 2
    assert len(remote.writes("/issues")) == len(remote.writes("/pulls")) == 1
    assert len(remote.writes("/git/refs")) == 1
    commits = [json.loads(request.content) for request in remote.writes("/git/commits")]
    assert commits[0] == commits[1]
    assert all(request.method != "PATCH" for request in remote.calls)
    assert not any("merge" in request.url.path for request in remote.calls)
    with publisher.store.connection() as db:
        assert TOKEN_A not in "\n".join(db.iterdump())


def test_lost_pr_response_is_reconciled_without_a_second_post(publication):
    publisher, task, patch, remote = publication
    remote.lost_pr = True
    with pytest.raises(GitHubError, match="github_unreachable"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    result = publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert result["pr"]["number"] == 2
    assert len(remote.writes("/pulls")) == 1


def test_repository_url_casing_uses_github_canonical_name_for_remote_links(publication):
    publisher, task, patch, _ = publication
    with publisher.store.connection() as db:
        db.execute(
            "UPDATE repositories SET name='UPSTREAM/project',"
            "url='https://github.com/UPSTREAM/project' WHERE id=?", (task["repo_id"],),
        )
    result = publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert result["repository"] == "upstream/project"
    assert result["pr"]["url"] == "https://github.com/upstream/project/pull/2"


def test_unresolved_post_intent_never_blindly_replays(publication):
    publisher, task, patch, remote = publication
    remote.missing_pr = True
    with pytest.raises(GitHubError):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    remote.missing_pr = False
    with pytest.raises(Conflict, match="repair_publication_uncertain"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert len(remote.writes("/pulls")) == 1


def test_upstream_changes_reject_stale_repair_before_any_write(publication):
    publisher, task, patch, remote = publication
    remote.revision = "f" * 40
    with pytest.raises(Conflict, match="repair_upstream_changed"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert not [request for request in remote.calls if request.method == "POST"]


def test_remote_source_blob_must_match_the_locally_tested_original(publication):
    publisher, task, patch, remote = publication
    remote.original_blob = "f" * 40
    with pytest.raises(Conflict, match="repair_source_changed"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert not [request for request in remote.calls if request.method == "POST"]


def test_public_fork_is_verified_and_used_when_upstream_push_is_denied(publication):
    publisher, task, patch, remote = publication
    remote.push = False
    result = publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert result["repository"] == "alice/project"
    assert len(remote.writes("/forks")) == 1
    assert all("/repos/alice/project/" in request.url.path
               for request in remote.writes("/git/blobs"))
    assert "/repos/upstream/project/pulls" == remote.writes("/pulls")[0].url.path


@pytest.mark.parametrize("private,unrelated,error", [
    (True, False, "repair_fork_not_allowed"), (False, True, "repair_fork_conflict"),
])
def test_fork_policy_rejects_private_or_unrelated_repositories(
    publication, private, unrelated, error,
):
    publisher, task, patch, remote = publication
    remote.push, remote.private, remote.unrelated_fork = False, private, unrelated
    with pytest.raises(Conflict, match=error):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert not remote.writes("/forks") and not remote.writes("/pulls")


def test_marker_reconciliation_checks_author_and_branch_identity(publication):
    publisher, task, patch, remote = publication
    publisher.publish(task, patch, "Synthetic", check=lambda: None)
    remote.branch_conflict = True
    with pytest.raises(Conflict, match="repair_remote_conflict"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert len(remote.writes("/pulls")) == 1


def test_account_switch_blocks_publication_before_api_writes(publication):
    publisher, task, patch, remote = publication
    with publisher.store.connection() as db:
        db.execute("UPDATE github_configuration SET active_account_id=NULL")
    with pytest.raises(Conflict, match="repair_account_changed"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert not [request for request in remote.calls if request.method == "POST"]


@pytest.mark.parametrize("path", ["https://evil.example/repos/x/y", "//evil.example", "user"])
def test_rest_client_never_sends_credentials_to_an_external_url(path):
    client = GitHubClient(httpx.MockTransport(lambda request: pytest.fail("must not send request")))
    with pytest.raises(GitHubError, match="github_invalid_input"):
        client.request_json("GET", path, TOKEN_A)


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limited_mutation_is_rejected_and_explicit_retry_can_reconcile(publication, status):
    publisher, task, patch, remote = publication
    original = remote.handle
    limited = True

    def handle(request):
        nonlocal limited
        if request.method == "POST" and request.url.path.endswith("/pulls") and limited:
            limited = False
            return httpx.Response(status, headers={"retry-after": "60"},
                                  json={"message": "secondary rate limit"})
        return original(request)

    publisher.github.client = GitHubClient(httpx.MockTransport(handle))
    with pytest.raises(GitHubError, match="github_rate_limited"):
        publisher.publish(task, patch, "Synthetic", check=lambda: None)
    with publisher.store.connection() as db:
        operation = db.execute(
            "SELECT state FROM repair_remote_operations WHERE repair_id=? AND operation='pr'",
            (task["id"],),
        ).fetchone()
    assert operation["state"] == "rejected"
    result = publisher.publish(task, patch, "Synthetic", check=lambda: None)
    assert result["pr"]["number"] == 2 and len(remote.prs) == 1
