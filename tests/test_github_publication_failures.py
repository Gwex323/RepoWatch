"""Access diagnostics and explicit publication recovery; all remote calls are mocked."""

import asyncio
import json

import httpx
import pytest
from test_execution import service  # noqa: F401
from test_repairs import confirm, enable, pending  # noqa: F401

from repowatch.github_accounts import GitHubClient, GitHubError
from repowatch.repair_model import repair_failure_details
from repowatch.store import Conflict


@pytest.mark.parametrize("status,headers,message,code", [
    (403, {}, "Resource not accessible by personal access token", "github_permission_denied"),
    (403, {}, "Resource not accessible by integration", "github_permission_denied"),
    (403, {}, "organization restriction", "github_forbidden"),
    (429, {}, "", "github_rate_limited"),
    (403, {"x-ratelimit-remaining": "0"}, "", "github_rate_limited"),
    (403, {"retry-after": "60"}, "", "github_rate_limited"),
    (403, {}, "You have exceeded a secondary rate limit", "github_rate_limited"),
    (403, {}, "API rate limit exceeded", "github_rate_limited"),
    (403, {"x-ratelimit-remaining": "42"}, "denied", "github_forbidden"),
])
def test_github_access_errors_are_classified_without_raw_messages(status, headers, message, code):
    def handle(request):
        return httpx.Response(status, headers=headers, json={"message": message + " secret_value"})
    client = GitHubClient(httpx.MockTransport(handle))
    with pytest.raises(GitHubError) as caught:
        client.request_json("POST", "/repos/alice/project/git/blobs", "private_token", {})
    error = caught.value
    assert error.code == code
    assert error.status == (429 if code == "github_rate_limited" else 403)
    assert error.details["status"] == status
    assert error.details["operation"] == "git_blob"
    assert "private_token" not in repr(error.details) + str(error)
    assert "secret_value" not in repr(error.details) + str(error)


def test_github_permission_headers_are_allowlisted_and_survive_wrapped_failure():
    response = httpx.Response(403, json={"message": "Resource not accessible by integration"},
        headers={"x-accepted-github-permissions": "contents=write, issues=read,secret=write",
                 "x-ratelimit-remaining": "4977", "x-github-request-id": "ABCD:0123",
                 "x-ratelimit-reset": "1792059181"})
    error = GitHubClient.access_error(response, "POST", "/repos/private/secret/git/blobs")
    wrapper = RuntimeError("must not persist")
    wrapper.__cause__ = error
    details = repair_failure_details(wrapper)
    assert details["github"] == {
        "status": 403, "method": "POST", "operation": "git_blob", "remaining": 4977,
        "reset_at": 1792059181, "request_id": "ABCD:0123",
        "required_permissions": {"contents": "write", "issues": "read"},
    }
    assert "private/secret" not in json.dumps(details)
    assert "must not persist" not in json.dumps(details)
    invalid = GitHubClient.access_error(httpx.Response(403, content=b"not-json", headers={
        "x-accepted-github-permissions": "contents=secret", "retry-after": "secret",
        "x-ratelimit-remaining": "-1", "x-github-request-id": "secret/path",
    }), "GET", "/repos/alice/project")
    assert invalid.details == {"status": 403, "method": "GET", "operation": "read_access"}


def failed_publication(pending):  # noqa: F811
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    publisher.fail = True
    asyncio.run(repairs.tick())
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET attempt=9 WHERE id=?", (task["id"],))
    return repairs, repairs.get(task["id"]), runtime, publisher


def test_verified_publication_retry_bypasses_only_model_budget_and_preserves_history(pending):  # noqa: F811
    repairs, failed, runtime, publisher = failed_publication(pending)
    assert failed["publication_retry_available"] and failed["can_retry"]
    calls = list(runtime.test_calls)
    with repairs.store.connection() as db:
        db.execute("UPDATE github_accounts SET version=version+1")
    queued = repairs.retry(failed["id"], failed["version"])
    assert queued["context"]["account_version"] > failed["context"]["account_version"]
    assert queued["patch"] == failed["patch"]
    assert queued["attempts"][-1]["attempt"] == 9
    assert queued["context"]["publication_retries"] == 1
    publisher.fail = False
    asyncio.run(repairs.tick())
    result = repairs.get(failed["id"])
    assert result["status"] == "pr_created" and result["attempt"] == 10
    assert len(repairs.engine.models) == 1 and runtime.test_calls == calls
    assert not result["context"].get("resume_publication")


def test_publication_retry_limit_cannot_be_reset_by_normal_retry(pending):  # noqa: F811
    repairs, failed, runtime, publisher = failed_publication(pending)
    for used in range(1, 4):
        assert failed["publication_retry_available"]
        repairs.retry(failed["id"], failed["version"])
        asyncio.run(repairs.tick())
        failed = repairs.get(failed["id"])
        assert failed["context"]["publication_retries"] == used
        assert failed["attempt"] == 9 + used
    assert not failed["can_retry"] and not failed["publication_retry_available"]
    with pytest.raises(Conflict, match="repair_publication_retry_limit"):
        repairs.retry(failed["id"], failed["version"])
    assert len(repairs.engine.models) == 1 and len(publisher.calls) == 4


@pytest.mark.parametrize("change", ["unverified", "source", "account", "confirmation", "version"])
def test_publication_recovery_does_not_bypass_existing_gates(pending, change):  # noqa: F811
    repairs, failed, _, publisher = failed_publication(pending)
    with repairs.store.connection() as db:
        if change == "unverified":
            db.execute("UPDATE repair_tasks SET review=NULL WHERE id=?", (failed["id"],))
        elif change == "account":
            db.execute("UPDATE github_configuration SET active_account_id=NULL")
        elif change == "confirmation":
            db.execute("UPDATE analysis_findings SET status='unconfirmed'")
    if change == "source":
        source = repairs.execution.directory(failed["env_id"]) / "source/app.py"
        source.write_text("print('changed')", encoding="utf-8")
        repairs.retry(failed["id"], failed["version"])
        asyncio.run(repairs.tick())
        assert repairs.get(failed["id"])["error"] == "repair_source_changed"
    else:
        with pytest.raises(Conflict):
            repairs.retry(failed["id"], failed["version"] - int(change == "version"))
    assert len(publisher.calls) == 1 and len(repairs.engine.models) == 1


def test_publication_retry_diagnostics_are_persisted_without_replaying_model(pending):  # noqa: F811
    repairs, failed, _, publisher = failed_publication(pending)
    def denied(*args, **kwargs):
        raise GitHubClient.access_error(httpx.Response(403, json={
            "message": "Resource not accessible by personal access token secret_value",
        }, headers={"x-accepted-github-permissions": "contents=write"}),
            "POST", "/repos/alice/project/git/blobs")
    publisher.publish = denied
    repairs.retry(failed["id"], failed["version"])
    asyncio.run(repairs.tick())
    result = repairs.get(failed["id"])
    assert result["error"] == "github_permission_denied"
    details = [json.loads(e["value"]) for e in result["events"] if e["kind"] == "failure_details"]
    assert details[-1]["github"]["required_permissions"] == {"contents": "write"}
    assert "secret_value" not in json.dumps(result)


@pytest.mark.parametrize("case", ["budget", "stage", "duplicate", "unverified_claim"])
def test_publication_allowance_is_bounded_one_use_and_cannot_expand_model_work(pending, case):  # noqa: F811
    repairs, failed, _, publisher = failed_publication(pending)
    if case == "duplicate":
        queued = repairs.retry(failed["id"], failed["version"])
        with pytest.raises(Conflict):
            repairs.retry(failed["id"], failed["version"])
        assert repairs.get(failed["id"])["context"]["publication_retries"] == 1
        assert len(queued["attempts"]) == 1
    else:
        with repairs.store.connection() as db:
            context = json.loads(db.execute(
                "SELECT context FROM repair_tasks WHERE id=?", (failed["id"],),
            ).fetchone()[0])
            if case == "budget":
                context["publication_retries"] = 3
                db.execute("UPDATE repair_tasks SET attempt=1,context=? WHERE id=?",
                           (json.dumps(context), failed["id"]))
            elif case == "stage":
                db.execute("UPDATE repair_tasks SET stage='repair_agent' WHERE id=?",
                           (failed["id"],))
            else:
                context.update(resume_publication=True, publication_retries=1)
                db.execute(
                    "UPDATE repair_tasks SET status='queued',review=NULL,context=? WHERE id=?",
                    (json.dumps(context), failed["id"]),
                )
        if case == "unverified_claim":
            assert repairs.claim() is None
            assert repairs.get(failed["id"])["error"] == "repair_attempt_limit"
        else:
            assert not repairs.get(failed["id"])["can_retry"]
            with pytest.raises(Conflict):
                repairs.retry(failed["id"], failed["version"])
    assert len(repairs.engine.models) == 1 and len(publisher.calls) == 1


def test_permission_alternatives_are_not_misrepresented_as_combined_requirements():
    error = GitHubClient.access_error(httpx.Response(403, headers={
        "x-accepted-github-permissions": "issues=write;pull_requests=write",
    }), "POST", "/repos/alice/project/issues")
    assert "required_permissions" not in error.details
    assert error.details["permission_alternatives"] == [
        {"issues": "write"}, {"pull_requests": "write"},
    ]
