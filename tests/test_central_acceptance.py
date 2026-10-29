"""Cross-stage acceptance with real workers/SDK and isolated remote/runtime doubles."""

import asyncio
import json
import runpy
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from test_analysis_handoff_recovery import EarlyAnswerModel
from test_analysis_tool_arguments import ITEM, response
from test_coordination import Model as CoordinationModel
from test_execution import Runtime, Source
from test_maintenance import PATH, SOURCE, maintenance  # noqa: F401
from test_releases import release, save  # noqa: F401
from test_repository_analysis import Engine, prepare_tested

from repowatch import recovery
from repowatch.coordination import Coordinator
from repowatch.execution import Execution
from repowatch.github_accounts import GitHubClient
from repowatch.release_github import ReleaseAuthority
from repowatch.release_manifest import Manifest, git
from repowatch.release_runtime import NativeRuntime
from repowatch.release_state import Journal, verifies_released_source
from repowatch.releases import ReleaseManager, Releases
from repowatch.repository_analysis import ROLES, Analyses, run_analysis
from repowatch.store import Conflict, Store
from repowatch.system_faults import SystemFaults
from repowatch.task_scopes import scope_packet


class JourneyEngine(Engine):
    def __init__(self, implementation):
        self.implementation = implementation
        self.models = []

    async def analyze(self, context, event):
        broken = not runpy.run_path(str(self.implementation))["bounded"](2)
        for role in ROLES:
            event("model_configuration", json.dumps({
                "role": role, "model": "isolated-journey", "model_version": 1,
                "configuration_version": 1,
            }))

        class ScopeModel(EarlyAnswerModel):
            async def get_response(self, *args, **kwargs):
                if kwargs["model_settings"].tool_choice == "register_task_scope":
                    self.calls += 1
                    batches = [("register_task_scope", {"items": [ITEM]}),
                               ("register_task_scope", {"items": [{**ITEM, "key": "second"}]})]
                    if broken:
                        batches[1][1]["items"][0]["paths"] = ["missing.py"]
                    return response(*batches)
                return await super().get_response(*args, **kwargs)

        model = ScopeModel([ROLES[1]])
        self.models.append(model)
        return await run_analysis(model, context, event)


def test_fault_to_reviewed_release_to_original_verification(
        maintenance, release, monkeypatch, record_property):  # noqa: F811
    maintainer, remote, _ = maintenance
    store, started, trace = maintainer.store, time.monotonic(), []
    execution = Execution(store, maintainer.github, runtime=Runtime(), source=Source())
    repo = store.add_repository("owner/sample", "https://github.com/owner/sample")
    environment = prepare_tested((execution, repo, execution.runtime))
    code = Path(release.manifest.code_root)
    implementation = code / PATH
    implementation.parent.mkdir()
    implementation.write_text(SOURCE, encoding="utf-8")
    engine = JourneyEngine(implementation)
    analyses = Analyses(execution, engine)
    coordinator = Coordinator(store, CoordinationModel(), maintainer.repairs)
    faults = SystemFaults(store)

    assert not maintainer.state()["enabled"] and not coordinator.state()["enabled"]
    with store.connection() as db:
        assert not recovery.state(db)["enabled"]
        assert db.execute("SELECT COUNT(*) FROM release_authorizations").fetchone()[0] == 0
        recovery.configure(db, True, 1)
    coordinator.configure(version=1, enabled=True)
    maintainer.configure(version=1, enabled=True)
    task_id = analyses.enqueue(environment["id"])
    asyncio.run(analyses.tick())
    assert analyses.get(task_id)["status"] == "failed"
    faults.tick()
    with store.connection() as db:
        occurrence = db.execute("SELECT * FROM system_fault_occurrences WHERE task_id=? "
                                "ORDER BY occurred DESC LIMIT 1", (task_id,)).fetchone()
        assert occurrence is not None and occurrence["state"] == "blocked"
        assert not scope_packet(db, task_id)["items"]
        before = recovery.snapshot(db, task_id)["budget"]
    trace.append("source_fault_recorded_without_partial_scope")

    asyncio.run(coordinator.tick())
    assert coordinator.list()[0]["state"] == "blocked"
    assert len(coordinator.notifications()) == 1
    trace.append("coordinator_blocked_without_inventing_analysis_retry")
    remote.lose_pr = True
    asyncio.run(maintainer.tick())
    task = maintainer.list()[0]
    assert task["state"] == "blocked" and len(remote.prs) == 1
    Store(store.path)
    maintainer.retry_publication(task["id"], task["version"])
    task = maintainer.list()[0]
    assert task["state"] == "pr_created", task["error"]
    assert [item["review"]["approved"] for item in
            task["artifacts"]["review_history"]] == [False, True]
    assert task["artifacts"]["verification"]["passed"]
    assert len(remote.issues) == len(remote.prs) == 1
    assert task["branch"].startswith("feature/")
    assert remote.prs[0]["base"]["ref"] == "actual-main"
    for heading in ("概述", "变更内容", "设计方案/说明", "验证/验收", "review记录"):
        assert heading in remote.prs[0]["body"]
    trace.append("issue_feature_patch_rejected_corrected_reviewed_pr_reconciled")

    # Reopening the real database and replaying collectors must not restart the work.
    reopened = Store(store.path)
    SystemFaults(reopened).tick()
    asyncio.run(maintainer.tick())
    assert len(remote.issues) == len(remote.prs) == 1
    with reopened.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM maintenance_tasks").fetchone()[0] == 1
        assert recovery.snapshot(db, task_id)["budget"]["requests"] >= before["requests"]
        assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                          (occurrence["id"],)).fetchone()[0] != "resolved"
    trace.append("reopen_and_replay_preserve_one_publication_and_root_budget")

    # The remote is HTTP-only and the checkout is a temporary local Git repository.
    # Merge and deploy still pass the production authorization/receipt adapters.
    for edit in task["artifacts"]["patch"]["edits"]:
        destination = code / edit["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(edit["content"], encoding="utf-8")
    for arguments in (["add", "."], ["-c", "user.name=Fixture", "-c",
            "user.email=fixture@localhost", "commit", "-qm", "reviewed fixture repair"]):
        subprocess.run(["git", "-C", str(code), *arguments], check=True, capture_output=True)
    commit = git(code, "rev-parse", "HEAD")
    original_handle = remote.handle
    merge_puts = []

    def handle(request):
        path = request.url.path
        if path == "/graphql":
            head = remote.prs[0]["head"]["sha"]
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": head, "baseRefName": "actual-main", "isDraft": False,
                "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "reviewDecision": "APPROVED", "commits": {"nodes": [{"commit": {
                    "oid": head, "statusCheckRollup": {"state": "SUCCESS"}}}]},
            }}}}
            return httpx.Response(200, json=value)
        if path.endswith("/merge"):
            merge_puts.append(json.loads(request.content))
            remote.prs[0].update(merged_at="2026-10-29T00:00:00Z", merged=True,
                                 merge_commit_sha=commit)
            remote.prs[0]["base"]["repo"] = {"id": 99}
            raise httpx.ReadError("merge succeeded but receipt was lost")
        if "/compare/" in path:
            return httpx.Response(200, json={"status": "identical"})
        return original_handle(request)

    maintainer.github.client = GitHubClient(httpx.MockTransport(handle))
    authority, journal = ReleaseAuthority(store, maintainer.github), Journal(release.root)
    head = remote.prs[0]["head"]["sha"]
    with pytest.raises(Conflict, match="merge_authorization_required"):
        authority.merge(1, head, journal)
    binding = authority.authorize("merge", head)
    with pytest.raises(Exception):
        authority.merge(1, head, journal)
    assert authority.merge(1, head, journal)["commit"] == commit
    assert len(merge_puts) == 1
    maintainer.reconcile(task["id"], maintainer.list()[0]["version"])
    trace.append("separate_merge_grant_and_lost_receipt_reconciled_once")

    manifest = Manifest.build(
        root=Path(release.manifest.code_root), python=Path(release.manifest.python),
        database=store.path.resolve(), config_ref=Path(release.manifest.config_ref),
        state_root=release.root, port=release.manifest.port, pr=1, commit=commit,
        binding=binding, maintenance_task=task["id"], workers=False)
    path = save(manifest)
    manager = ReleaseManager(authority, runtime_factory=release.Runtime,
                             migration_probe=lambda *_: None)
    with pytest.raises(Conflict, match="deploy_authorization_required"):
        manager.deploy(path)
    authority.authorize("deploy", manifest.id, resume_original=True)
    manager.deploy(path)
    journal.deliver(store)
    monkeypatch.setattr(store, "workbench_version", "d" * 32)
    monkeypatch.setattr(NativeRuntime, "health", lambda _: release.Runtime(manifest, path).health())
    resumed = Releases(store, maintainer, analyses, None, None, manifest)
    resumed.resume_original()
    resumed.resume_original()
    with store.connection() as db:
        row = db.execute("SELECT * FROM release_resumptions").fetchone()
        assert row["state"] == "verifying_original", row["reason"]
        assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                          (occurrence["id"],)).fetchone()[0] != "resolved"
        assert recovery.snapshot(db, task_id)["budget"]["requests"] >= before["requests"]
    trace.append("authorized_deploy_queues_original_once_without_resolving_fault")

    asyncio.run(analyses.tick())
    result = analyses.get(task_id)
    assert result["status"] == "completed", result["error"]
    assert len(engine.models[-1].recoveries) == 1
    with store.connection() as db:
        verified = json.loads(db.execute(
            "SELECT payload FROM system_fault_outbox WHERE kind='observation' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert verifies_released_source(db, occurrence, verified)
        for changed in ({"workbench_version": "unrelated"}, {"task_id": "other"},
                        {"attempt": occurrence["attempt"]}, {"state": "blocked"},
                        {"occurred": occurrence["occurred"]}):
            assert not verifies_released_source(db, occurrence, {**verified, **changed})
        db.execute("UPDATE release_authorizations SET revoked=1 WHERE kind='deploy'")
        assert not verifies_released_source(db, occurrence, verified)
        db.execute("UPDATE release_authorizations SET revoked=0 WHERE kind='deploy'")
    faults.tick()
    resumed.reconcile_resumptions()
    with store.connection() as db:
        assert {i["key"] for i in scope_packet(db, task_id)["items"]} == {"behavior", "second"}
        state = db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                           (occurrence["id"],)).fetchone()[0]
        assert state == "resolved", state
        assert db.execute("SELECT state FROM release_resumptions").fetchone()[0] == "completed"
        budget = recovery.snapshot(db, task_id)["budget"]
        assert budget["requests"] > before["requests"]
    trace.append("original_two_scopes_native_handoff_and_review_verify_fault")

    coordinator.configure(version=2, enabled=False)
    maintainer.configure(version=2, enabled=False)
    with store.connection() as db:
        recovery.configure(db, False, 2)
    assert coordinator.claim() is None and maintainer.claim() is None
    assert not any(method != "GET" and not path.startswith("/repos/Gwex323/RepoWatch/")
                   for method, path in remote.calls)
    record_property("acceptance_trace", json.dumps(trace))
    record_property("acceptance_metrics", json.dumps({
        "source_faults": 1, "source_verified": 1, "requests_before": before["requests"],
        "requests_after": budget["requests"], "tokens_reported": budget["tokens"],
        "token_usage_complete": False, "known_cost": budget.get("cost"),
        "duration_seconds": time.monotonic() - started,
        "issue_posts": len(remote.issues), "pr_posts": len(remote.prs),
        "merge_puts": len(merge_puts),
        "explicit_grants": ["rules", "coordinator", "maintenance", "merge_head", "deploy_manifest"],
        "owner_reconciliations": ["lost_pr_receipt", "lost_merge_receipt"],
        "live_models": False, "native_runtime": False,
    }))
