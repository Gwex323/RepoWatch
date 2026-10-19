import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import Runtime, Source, prepare, service  # noqa: F401
from test_github_accounts import TOKEN_A, MemoryVault, Remote
from test_repository_analysis import Engine, prepare_tested  # noqa: F401

from repowatch.app import create_app
from repowatch.github_accounts import GitHubError
from repowatch.historical_review import historical_snapshot
from repowatch.memory import capture_repair_fix
from repowatch.repairs import Repairs
from repowatch.repository_analysis import Analyses
from repowatch.store import Conflict

PR_URL = "https://github.com/alice/project/pull/7"


class StubGithub:
    def __init__(self, *, merged=True, fail=False):
        self.merged, self.fail, self.requests = merged, fail, []
        self.client = self

    def validated_identity(self, account_id, version):
        assert account_id == 101 and version is None
        return "token-a", {"id": 101, "login": "alice"}

    def request_json(self, method, path, token, payload=None):
        self.requests.append((method, path))
        if self.fail:
            raise GitHubError("github_unreachable", 502)
        return {"merged": self.merged, "html_url": PR_URL}, {}


class StubPublisher:
    def __init__(self, github):
        self.github = github


def published_repair(execution, finding_id):
    with execution.store.connection() as db:
        db.execute(
            "UPDATE repair_tasks SET status='pr_created',patch=?,verification=?,review=?,"
            "remote=? WHERE finding_id=?",
            (json.dumps({
                "summary": "修复边界处理并新增回归测试",
                "edits": [{"path": "app.py", "content": "fixed",
                           "original_sha256": "a" * 64},
                          {"path": "tests/test_regression.py", "content": "def test(): pass",
                           "original_sha256": ""}],
                "regression_paths": ["tests/test_regression.py"],
                "regression_command": "python -m pytest tests/test_regression.py",
            }),
             json.dumps({"regression": {"exit_code": 0}, "full": {"exit_code": 0}}),
             json.dumps({"approved": True, "summary": "差异与测试证据一致。",
                         "risks": ["样例风险"]}),
             json.dumps({
                "repository": "alice/project", "branch": "repowatch/x", "commit": "a" * 40,
                "issue": {"number": 6, "url": "https://github.com/alice/project/issues/6"},
                "pr": {"number": 7, "url": PR_URL}, "account_id": 101,
                "account_login": "alice",
            }), finding_id),
        )


def fix_memories(execution, repo_id):
    with execution.store.connection() as db:
        return db.execute(
            "SELECT * FROM memories WHERE repo_id=? AND category='fix'", (repo_id,),
        ).fetchall()


def analyzed(execution_fixture):
    execution, _, _ = execution_fixture
    env = prepare_tested(execution_fixture)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    return execution, analyses, env, row


def test_stale_waiting_finding_can_be_disposed_and_leaves_candidates(service):  # noqa: F811
    execution, analyses, env, row = analyzed(service)
    finding = row["findings"][0]
    execution.configure(env["id"], env["version"], {**env["config"], "test": "echo changed"})
    assert not analyses.get(row["id"])["current"]
    result = analyses.disposition(row["id"], finding["id"], finding["version"],
                                  "not_reproduced", " 误报 ")
    saved = result["findings"][0]
    assert saved["status"] == "not_reproduced"
    assert saved["version"] == finding["version"] + 1
    assert saved["feedback"]["note"] == "误报"
    assert saved["feedback"]["actor"] == "owner"
    assert saved["feedback"]["previous_status"] == "waiting_reproduction"
    assert saved["feedback"]["disposition"] is True
    with execution.store.connection() as db:
        repo_id = db.execute(
            "SELECT repo_id FROM repository_analyses WHERE id=?", (row["id"],),
        ).fetchone()["repo_id"]
        snapshot = historical_snapshot(db, repo_id, "analysis_other", 9e12)
    assert snapshot["total"] == 0 and snapshot["items"] == []


def test_confirmed_finding_disposition_cancels_queued_repair(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    repairs = Repairs(execution)
    analyses = Analyses(execution, Engine(), repairs)
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    finding = row["findings"][0]
    analyses.feedback(row["id"], finding["id"], finding["version"], "confirmed", "", True)
    with execution.store.connection() as db:
        repair = db.execute("SELECT * FROM repair_tasks WHERE finding_id=?",
                            (finding["id"],)).fetchone()
    assert repair and repair["status"] == "queued"
    result = analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                                  "not_reproduced", "")
    assert result["findings"][0]["status"] == "not_reproduced"
    assert result["findings"][0]["feedback"]["previous_status"] == "confirmed"
    with execution.store.connection() as db:
        repair = db.execute("SELECT * FROM repair_tasks WHERE finding_id=?",
                            (finding["id"],)).fetchone()
        events = db.execute("SELECT kind,value FROM repair_events WHERE repair_id=?",
                            (repair["id"],)).fetchall()
    assert repair["status"] == "cancelled"
    assert [(item["kind"], item["value"]) for item in events] == [
        ("queued", "confirmed_finding"), ("cancelled", "finding_disposition"),
    ]


def test_running_repair_blocks_disposition(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    repairs = Repairs(execution)
    analyses = Analyses(execution, Engine(), repairs)
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    finding = row["findings"][0]
    analyses.feedback(row["id"], finding["id"], finding["version"], "confirmed", "", True)
    with execution.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='running' WHERE finding_id=?", (finding["id"],))
    with pytest.raises(Conflict, match="repair_running"):
        analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                             "not_reproduced", "")


def test_disposition_rejects_stale_versions_and_closed_findings(service):  # noqa: F811
    execution, analyses, _, row = analyzed(service)
    finding = row["findings"][0]
    with pytest.raises(Conflict, match="finding_changed"):
        analyses.disposition(row["id"], finding["id"], finding["version"] + 5,
                             "not_reproduced", "")
    result = analyses.disposition(row["id"], finding["id"], finding["version"],
                                  "not_reproduced", "")
    assert result["findings"][0]["status"] == "not_reproduced"
    with pytest.raises(Conflict, match="finding_closed"):
        analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                             "not_reproduced", "")


def test_live_status_is_joined_into_historical_review(service):  # noqa: F811
    execution, analyses, _, row = analyzed(service)
    finding = row["findings"][0]
    analyses.disposition(row["id"], finding["id"], finding["version"],
                         "not_reproduced", "确认误报")
    snapshot_item = {
        "finding_id": finding["id"], "version": finding["version"],
        "human_status": "confirmed", "analysis_id": "analysis_older",
        "revision": "a" * 40, "data": {"title": "Legacy claim"},
        "content_clipped": False,
    }
    with execution.store.connection() as db:
        db.execute("UPDATE repository_analyses SET report=?,context=? WHERE id=?", (
            json.dumps({"summary": "Fixture.", "limitations": [], "findings": [],
                        "files_read": [], "execution_read": True, "handoffs": []}),
            json.dumps({"historical_findings": {
                "total": 1, "included": 1, "omitted": 0, "items": [snapshot_item]}}),
            row["id"],
        ))
    review = analyses.get(row["id"])["historical_review"]
    assert review["items"][0]["human_status"] == "confirmed"
    assert review["items"][0]["live_status"] == "not_reproduced"
    assert review["items"][0]["live_version"] == finding["version"] + 1


def test_disposition_api_requires_owner_csrf_and_rejects_forged_fields(tmp_path):
    app = create_app(
        Path(tmp_path) / "api.db", start_worker=False, github_client=Remote().client,
        credential_vault=MemoryVault(), execution_runtime=Runtime(),
        repository_source=Source(), analysis_engine=Engine(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        execution = app.state.execution
        execution.github.connect("alice", TOKEN_A)
        repo = execution.store.add_repository("alice/project", "https://github.com/alice/project")
        env = prepare_tested((execution, repo, execution.runtime))
        url = f"/api/environments/{env['id']}/analyses"
        client.post(url, headers=headers, json={"version": env["version"]})
        asyncio.run(app.state.analyses.tick())
        row = client.get(url).json()[0]
        finding = row["findings"][0]
        disposition_url = f"/api/analyses/{row['id']}/findings/{finding['id']}/disposition"
        assert client.post(disposition_url, json={
            "version": 1, "decision": "not_reproduced"}).status_code == 403
        forged = client.post(disposition_url, headers=headers, json={
            "version": 1, "decision": "confirmed",
            "note": "secret-should-not-echo", "unexpected": True})
        assert forged.status_code == 422 and "secret-should-not-echo" not in forged.text
        assert client.post(disposition_url, headers=headers,
                           json={"version": 999, "decision": "not_reproduced"}).status_code == 409
        accepted = client.post(disposition_url, headers=headers,
                               json={"version": 1, "decision": "not_reproduced", "note": "误报"})
        assert accepted.status_code == 200
        assert accepted.json()["findings"][0]["status"] == "not_reproduced"
        assert client.post(disposition_url, headers=headers,
                           json={"version": 2, "decision": "not_reproduced"}).status_code == 409


def test_resolved_owner_assertion_requires_note_and_exits_candidates(service):  # noqa: F811
    execution, analyses, _, row = analyzed(service)
    finding = row["findings"][0]
    with pytest.raises(Conflict, match="resolution_note_required"):
        analyses.disposition(row["id"], finding["id"], finding["version"], "resolved", "  ")
    result = analyses.disposition(row["id"], finding["id"], finding["version"],
                                  "resolved", " 已在本地手动修复并验证 ")
    saved = result["findings"][0]
    assert saved["status"] == "resolved"
    assert saved["feedback"]["resolution"] == "owner_assertion"
    assert saved["feedback"]["note"] == "已在本地手动修复并验证"
    assert saved["feedback"]["previous_status"] == "waiting_reproduction"
    with execution.store.connection() as db:
        repo_id = db.execute(
            "SELECT repo_id FROM repository_analyses WHERE id=?", (row["id"],),
        ).fetchone()["repo_id"]
        snapshot = historical_snapshot(db, repo_id, "analysis_other", 9e12)
    assert snapshot["total"] == 0 and snapshot["items"] == []


def confirmed_with_repairs(execution_fixture, *, merged=True, fail=False):
    execution, _, _ = execution_fixture
    env = prepare_tested(execution_fixture)
    github = StubGithub(merged=merged, fail=fail)
    repairs = Repairs(execution, publisher=StubPublisher(github))
    analyses = Analyses(execution, Engine(), repairs)
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    finding = row["findings"][0]
    analyses.feedback(row["id"], finding["id"], finding["version"], "confirmed", "", True)
    return execution, analyses, row, finding, github


def test_resolved_with_merged_pr_uses_remote_evidence(service):  # noqa: F811
    execution, analyses, row, finding, github = confirmed_with_repairs(service)
    published_repair(execution, finding["id"])
    with execution.store.connection() as db:
        repo_id = db.execute(
            "SELECT repo_id FROM repository_analyses WHERE id=?", (row["id"],),
        ).fetchone()["repo_id"]
        repair_id = db.execute(
            "SELECT id FROM repair_tasks WHERE finding_id=?", (finding["id"],),
        ).fetchone()["id"]
    result = analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                                  "resolved", "")
    saved = result["findings"][0]
    assert saved["status"] == "resolved"
    assert saved["feedback"]["resolution"] == "merged_pr"
    assert saved["feedback"]["pr"] == PR_URL
    assert github.requests == [("GET", "/repos/alice/project/pulls/7")]
    with execution.store.connection() as db:
        repair = db.execute("SELECT status FROM repair_tasks WHERE finding_id=?",
                            (finding["id"],)).fetchone()
    assert repair["status"] == "pr_created"
    memories = fix_memories(execution, repo_id)
    assert len(memories) == 1
    memory = memories[0]
    assert memory["source"] == "workflow"
    assert memory["source_key"] == f"repair:{repair_id}"
    assert memory["revision"] == row["revision"]
    assert memory["title"].startswith("修复经验：")
    assert "修复 PR 已由 owner 核验合并" in memory["content"]
    evidence = json.loads(memory["evidence"])
    assert evidence["pr"] == PR_URL and evidence["merged"] is True
    assert evidence["repair_id"] == repair_id
    assert evidence["finding_id"] == finding["id"]


def test_merged_pr_fix_memory_is_deduplicated(service):  # noqa: F811
    execution, analyses, row, finding, _ = confirmed_with_repairs(service)
    published_repair(execution, finding["id"])
    analyses.disposition(row["id"], finding["id"], finding["version"] + 1, "resolved", "")
    with execution.store.connection() as db:
        repair = db.execute("SELECT * FROM repair_tasks WHERE finding_id=?",
                            (finding["id"],)).fetchone()
        capture_repair_fix(db, repair)
        capture_repair_fix(db, repair)
    assert len(fix_memories(execution, row["repo_id"])) == 1


def test_assertion_and_not_reproduced_do_not_write_fix_memory(service):  # noqa: F811
    execution, analyses, _, row = analyzed(service)
    finding = row["findings"][0]
    analyses.disposition(row["id"], finding["id"], finding["version"],
                         "resolved", "手动修复")
    assert fix_memories(execution, row["repo_id"]) == []


def test_fix_memory_loads_into_next_analysis_context(service):  # noqa: F811
    execution, analyses, row, finding, _ = confirmed_with_repairs(service)
    published_repair(execution, finding["id"])
    analyses.disposition(row["id"], finding["id"], finding["version"] + 1, "resolved", "")
    env = execution.get(row["env_id"])
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    asyncio.run(analyses.tick())
    newer = analyses.list(env["id"])[0]
    assert newer["id"] != row["id"]
    references = analyses.get(newer["id"])["memory_references"]
    assert any(item["title"].startswith("修复经验：") for item in references)


@pytest.mark.parametrize("merged,fail,code", [
    (False, False, "resolution_pr_not_merged"),
    (True, True, "resolution_verification_failed"),
])
def test_resolved_with_unverifiable_pr_is_rejected(service, merged, fail, code):  # noqa: F811
    execution, analyses, row, finding, github = confirmed_with_repairs(
        service, merged=merged, fail=fail)
    published_repair(execution, finding["id"])
    with pytest.raises(Conflict, match=code):
        analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                             "resolved", "说明")
    assert github.requests == [("GET", "/repos/alice/project/pulls/7")]
    assert analyses.get(row["id"])["findings"][0]["status"] == "confirmed"


def test_resolved_with_queued_repair_cancels_and_uses_owner_assertion(service):  # noqa: F811
    execution, analyses, row, finding, _ = confirmed_with_repairs(service)
    result = analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                                  "resolved", "已在其他分支手动修复")
    saved = result["findings"][0]
    assert saved["status"] == "resolved"
    assert saved["feedback"]["resolution"] == "owner_assertion"
    with execution.store.connection() as db:
        repair = db.execute("SELECT status FROM repair_tasks WHERE finding_id=?",
                            (finding["id"],)).fetchone()
    assert repair["status"] == "cancelled"


def test_resolved_blocks_running_repair(service):  # noqa: F811
    execution, analyses, row, finding, _ = confirmed_with_repairs(service)
    with execution.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='running' WHERE finding_id=?",
                   (finding["id"],))
    with pytest.raises(Conflict, match="repair_running"):
        analyses.disposition(row["id"], finding["id"], finding["version"] + 1,
                             "resolved", "说明")
