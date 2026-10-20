import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_execution import Runtime, Source, prepare
from test_github_accounts import TOKEN_A, MemoryVault

from repowatch.app import create_app
from repowatch.github_accounts import GitHubAccounts, GitHubClient
from repowatch.github_issues import GitHubIssues
from repowatch.store import Store

ISSUE = {
    "number": 7,
    "title": "Crash when importing config",
    "body": "api_key=secret-value",
    "state": "open",
    "user": {"login": "alice"},
    "html_url": "https://github.com/alice/project/issues/7",
    "updated_at": "2026-10-20T10:00:00Z",
}
CLOSED_ISSUE = {
    "number": 9,
    "title": "Closed report",
    "body": "already closed",
    "state": "open",
    "user": {"login": "alice"},
    "html_url": "https://github.com/alice/project/issues/9",
    "updated_at": "2026-10-20T10:02:00Z",
}
PULL_REQUEST = {
    "number": 8,
    "title": "Fix crash",
    "body": "pull request",
    "state": "open",
    "user": {"login": "alice"},
    "html_url": "https://github.com/alice/project/pull/8",
    "updated_at": "2026-10-20T10:01:00Z",
    "pull_request": {"url": "https://api.github.com/repos/alice/project/pulls/8"},
}


class IssueRemote:
    def __init__(self, pages=None):
        self.client = GitHubClient(httpx.MockTransport(self.handle))
        self.pages = pages

    def handle(self, request):
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={"id": 101, "login": "alice", "type": "User"},
                headers={"X-OAuth-Scopes": "repo, read:user"},
            )
        if request.url.path == "/repos/alice/project":
            return httpx.Response(
                200, json={"full_name": "alice/project", "private": False}
            )
        if request.url.path == "/repos/alice/project/issues":
            if self.pages is not None:
                page = int(request.url.params["page"])
                return httpx.Response(200, json=self.pages[page - 1])
            return httpx.Response(200, json=[ISSUE, PULL_REQUEST])
        return httpx.Response(404, json={"message": "not found"})


class Engine:
    enabled = True

    def status(self):
        return {"enabled": True, "model": "test-only"}

    async def analyze(self, context, event):
        return {"summary": "unused", "limitations": [], "findings": []}


@pytest.fixture
def issue_service(tmp_path):
    store = Store(tmp_path / "issues.db")
    remote = IssueRemote()
    vault = MemoryVault()
    github = GitHubAccounts(store, remote.client, vault)
    github.connect("alice", TOKEN_A)
    repo = store.add_repository(
        "alice/project", "https://github.com/alice/project"
    )
    return store, github, repo, GitHubIssues(store, github), remote, vault


def test_issue_sync_filters_pull_requests_redacts_and_deduplicates(issue_service):
    store, _, repo, issues, _, _ = issue_service
    first = issues.sync(repo["id"])
    assert len(first) == 1
    assert first[0]["number"] == ISSUE["number"]
    assert first[0]["title"] == ISSUE["title"]
    assert "secret-value" not in first[0]["body"]
    assert first[0]["author"] == "alice"
    assert first[0]["url"] == ISSUE["html_url"]

    second = issues.sync(repo["id"])
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]

    with store.connection() as db:
        count = db.execute(
            "SELECT count(*) FROM github_issues WHERE repo_id=?", (repo["id"],)
        ).fetchone()[0]
    assert count == 1


def test_issue_sync_paginates_and_closes_missing_open_issues(issue_service):
    store, _, repo, issues, remote, _ = issue_service
    pull_requests = [
        {**PULL_REQUEST, "number": number} for number in range(100, 199)
    ]
    remote.pages = [[ISSUE, *pull_requests], [CLOSED_ISSUE], []]
    issues.sync(repo["id"])

    remote.pages = [pull_requests, []]
    result = issues.sync(repo["id"])

    assert [issue["state"] for issue in result] == ["closed", "closed"]
    with store.connection() as db:
        rows = {
            row["number"]: row["state"]
            for row in db.execute(
                "SELECT number,state FROM github_issues WHERE repo_id=?", (repo["id"],)
            ).fetchall()
        }
    assert rows == {ISSUE["number"]: "closed", CLOSED_ISSUE["number"]: "closed"}


def test_issue_context_is_bound_to_analysis_and_api(issue_service):
    store, github, repo, issues, remote, vault = issue_service
    app = create_app(
        store.path,
        start_worker=False,
        github_client=remote.client,
        credential_vault=vault,
        execution_runtime=Runtime(),
        repository_source=Source(),
        analysis_engine=Engine(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = client.post(
            "/api/setup", json={"password": "owner-test-password"}
        ).json()
        headers = {"x-csrf-token": auth["csrf"]}

        execution = app.state.execution
        environment = prepare((execution, repo, execution.runtime))
        execution.action(environment["id"], environment["version"], "test")
        execution.tick()
        environment = execution.get(environment["id"])

        synced = client.post(
            f"/api/repositories/{repo['id']}/issues/sync", headers=headers
        )
        assert synced.status_code == 202
        issue = synced.json()[0]

        response = client.post(
            f"/api/repositories/{repo['id']}/issues/{issue['id']}/analysis",
            headers=headers,
        )
        assert response.status_code == 202
        analysis = response.json()
        assert analysis["status"] in ("queued", "waiting_model")

        with store.connection() as db:
            row = db.execute(
                "SELECT context FROM repository_analyses WHERE id=?",
                (analysis["id"],),
            ).fetchone()
        context = json.loads(row["context"])
        assert context["issue"]["id"] == issue["id"]
        assert context["issue"]["number"] == ISSUE["number"]
        assert "secret-value" not in context["issue"]["body"]
