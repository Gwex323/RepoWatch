"""Focused phase-five contracts; no production DB, model or GitHub writes."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_github_accounts import TOKEN_A, MemoryVault
from test_system_faults import attempt, source_event

from repowatch import recovery
from repowatch.analysis_basis import execution_trigger
from repowatch.app import create_app
from repowatch.diagnosis import fingerprint
from repowatch.github_accounts import GitHubAccounts, GitHubClient
from repowatch.release_github import ReleaseAuthority
from repowatch.release_manifest import Manifest, asset_files, git
from repowatch.release_runtime import NativeRuntime, Supervisor, matching_process, process_identity
from repowatch.release_state import (
    ExistingStore,
    Journal,
    atomic_json,
    existing_database,
    read_json,
    require_source_authorization,
)
from repowatch.releases import ReleaseManager, Releases, data_binding
from repowatch.repository_analysis import Analyses
from repowatch.store import Conflict, Store
from repowatch.system_faults import SystemFaults
from repowatch.task_scopes import ensure_scope

BINDING = {"account_id": 101, "account_version": 1, "repository_id": 99}


def altered(manifest, **changes):
    value = {**manifest.model_dump(exclude={"id"}), **changes}
    return Manifest(id=recovery.digest(value), **value)


def grant(store, manifest, resume=False):
    with store.connection() as db:
        db.execute("INSERT OR REPLACE INTO release_authorizations VALUES('deploy',?,?,?,?,0)",
                   (manifest.id, json.dumps(manifest.binding), int(resume), time.time()))


def save(manifest):
    path = Path(manifest.state_root) / "manifests" / (manifest.id + ".json")
    atomic_json(path, manifest.model_dump())
    return path


@pytest.fixture
def release(tmp_path):
    code = tmp_path / "checkout"
    (code / "scripts").mkdir(parents=True)
    (code / "scripts/managed.py").write_text("# isolated fixture\n", encoding="utf-8")
    (code / ".gitignore").write_text("dist/\n", encoding="utf-8")
    for arguments in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture",
            "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(code), *arguments], check=True, capture_output=True)
    (code / "dist/assets").mkdir(parents=True)
    (code / "dist/index.html").write_text('<script src="/assets/app.js"></script>')
    (code / "dist/assets/app.js").write_text("console.log('fixture');")
    config = tmp_path / "private-config.json"
    atomic_json(config, {"REPOWATCH_LANGFUSE_ENABLED": "0"})
    store = Store(tmp_path / "original.db")
    with store.connection() as db:
        db.execute("INSERT INTO github_accounts(id,login,encrypted_token,state,checked_at,updated) "
                   "VALUES(101,'Gwex323','unusable','verified',?,?)", (time.time(), time.time()))
        db.execute("UPDATE github_configuration SET active_account_id=101")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    manifest = Manifest.build(
        root=code, python=Path(sys.executable).resolve(), database=store.path.resolve(),
        config_ref=config.resolve(), state_root=(tmp_path / "state").resolve(),
        port=port, pr=1, commit=git(code, "rev-parse", "HEAD"), binding=BINDING, workers=False)
    path = save(manifest)
    grant(store, manifest)
    live = SimpleNamespace(alive=None, starts=0, stops=0, failure=None)

    class Runtime:
        def __init__(self, candidate, _path):
            self.manifest = candidate

        def record(self):
            return None

        def start(self):
            live.starts += 1
            live.alive = self.manifest.id

        def stop(self):
            live.stops += 1
            live.alive = None

        def wait(self):
            if live.failure and live.failure(self.manifest):
                raise Conflict("release_health_mismatch")
            return self.health()

        def health(self):
            if live.alive != self.manifest.id:
                raise Conflict("release_process_not_running")
            return {"ready": True}

    class Authority:
        def require(self, kind, target, binding):
            assert kind == "deploy"
            with store.connection() as db:
                row = db.execute("SELECT * FROM release_authorizations WHERE kind=? AND target=? "
                                 "AND revoked=0", (kind, target)).fetchone()
            if not row or json.loads(row["binding"]) != binding:
                raise Conflict("release_deploy_authorization_required")

        def merged(self, pr, commit, binding):
            assert binding == BINDING and len(commit) == 40 and pr > 0

    manager = ReleaseManager(Authority(), runtime_factory=Runtime, migration_probe=lambda *_: None)
    return SimpleNamespace(store=store, manifest=manifest, path=path, manager=manager,
                           Runtime=Runtime, live=live, root=Path(manifest.state_root))


def test_manifest_pins_checkout_assets_database_and_explicit_config(release, monkeypatch):
    manifest = release.manifest
    monkeypatch.chdir(release.root.parent)
    assert manifest.command(release.path)[1:3] == [
        "-I", str(Path(manifest.code_root) / "scripts/managed.py")]
    manifest.validate_files()
    value = manifest.model_dump()
    value["port"] += 1
    atomic_json(release.path, value)
    with pytest.raises(Conflict, match="manifest_changed"):
        Manifest.load(release.path)
    save(manifest)
    assets = Path(manifest.code_root) / "dist/assets/app.js"
    original = assets.read_bytes()
    assets.write_bytes(b"wrong build")
    with pytest.raises(Conflict, match="frontend_changed"):
        manifest.validate_files()
    assets.write_bytes(original)
    atomic_json(manifest.config_ref, {"REPOWATCH_LANGFUSE_ENABLED": "1"})
    with pytest.raises(Conflict, match="configuration_changed"):
        manifest.validate_files()
    with existing_database(release.store.path) as db:
        before = data_binding(db)
    ExistingStore(release.store.path)
    with existing_database(release.store.path) as db:
        assert data_binding(db) == before


def test_remote_merge_is_separately_authorized_rule_checked_and_reconciled(tmp_path):
    state = {"merged": False, "clean": False, "head": "a" * 40, "puts": 0}
    commit = "b" * 40

    def handle(request):
        path = request.url.path
        if path == "/user":
            value = {"id": 101, "login": "Gwex323", "type": "User"}
        elif path == "/repos/Gwex323/RepoWatch":
            value = {"id": 99, "full_name": "Gwex323/RepoWatch", "default_branch": "main",
                     "permissions": {"push": True}}
        elif path == "/graphql":
            value = {"data": {"repository": {"pullRequest": {
                "headRefOid": state["head"], "baseRefName": "main", "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN" if state["clean"] else "BLOCKED",
                "reviewDecision": "APPROVED",
                "commits": {"nodes": [{"commit": {"oid": state["head"],
                                                  "statusCheckRollup": {"state": "SUCCESS"}}}]},
            }}}}
        elif path.endswith("/merge"):
            state["puts"] += 1
            state["merged"] = True
            raise httpx.ReadError("lost response")
        elif "/compare/" in path:
            value = {"status": "ahead"}
        else:
            value = {"merged_at": "2026-10-29T00:00:00Z" if state["merged"] else None,
                     "merged": state["merged"], "merge_commit_sha": commit,
                     "head": {"sha": state["head"]},
                     "base": {"ref": "main", "repo": {"id": 99}}}
        return httpx.Response(200, json=value)

    store = Store(tmp_path / "remote.db")
    github = GitHubAccounts(store, client=GitHubClient(httpx.MockTransport(handle)),
                            vault=MemoryVault())
    github.connect("Gwex323", TOKEN_A)
    authority, journal = ReleaseAuthority(store, github), Journal(tmp_path / "journal")
    with pytest.raises(Conflict, match="authorization_required"):
        authority.merge(1, state["head"], journal)
    authority.authorize("merge", state["head"])
    with pytest.raises(Conflict, match="protected_merge_not_ready"):
        authority.merge(1, state["head"], journal)
    state["clean"] = True
    with pytest.raises(Exception):
        authority.merge(1, state["head"], journal)
    assert authority.merge(1, state["head"], journal)["commit"] == commit
    assert state["puts"] == 1
    with pytest.raises(Conflict, match="merged_commit_required"):
        authority.merged(1, "c" * 40, BINDING)


def analysis(store, state="failed"):
    repo = store.add_repository("owner/sample", "https://github.com/owner/sample")
    now, env, task, revision = time.time(), "env_" + "1" * 16, "analysis_" + "2" * 16, "a" * 40
    config = {"language": "python", "test": "pytest"}
    basis = fingerprint(config, revision)
    with store.connection() as db:
        db.execute("INSERT INTO environments(id,repo_id,account_id,account_version,revision,config,"
                   "state,created,updated) VALUES(?,?,101,1,?,?,'ready',?,?)",
                   (env, repo["id"], revision, json.dumps(config), now, now))
        db.execute("INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,"
                   "trigger_key,status,context,error,created,updated) "
                   "VALUES(?,?,?,?,?,?,?,'{}','analysis_scope_read_not_followed',?,?)",
                   (task, env, repo["id"], revision, basis, execution_trigger(basis, []),
                    state, now, now))
        ensure_scope(db, db.execute("SELECT * FROM repository_analyses WHERE id=?",
                                    (task,)).fetchone())
    return task, repo["id"], env


def test_drain_backup_and_probe_block_without_mutating_original_budgets(release):
    task, _, _ = analysis(release.store, "running")
    with pytest.raises(Conflict, match="active_tasks"):
        release.manager.deploy(release.path, drain_seconds=0)
    assert release.live.starts == release.live.stops == 0
    with release.store.connection() as db:
        assert not release.store.draining(db)
        db.execute("UPDATE repository_analyses SET status='failed' WHERE id=?", (task,))
        before = data_binding(db)

    def destructive_probe(_, __, copy):
        with existing_database(copy) as db:
            db.execute("UPDATE repair_settings SET enabled=1-enabled")

    release.manager.migration_probe = destructive_probe
    with pytest.raises(Conflict, match="migration_incompatible"):
        release.manager.deploy(release.path)
    with release.store.connection() as db:
        assert data_binding(db, before) == before
    release.manager.migration_probe = lambda *_: None
    assert release.manager.deploy(release.path)["state"] == "healthy"
    with release.store.connection() as db:
        assert data_binding(db, before) == before
    assert len(list((release.root / "backups").glob("*.db"))) >= 2


@pytest.mark.parametrize("cleanup_error", [None, "release_process_identity_changed"])
def test_first_deployment_failure_cleans_only_identified_candidate(release, monkeypatch,
                                                                  cleanup_error):
    release.live.failure = lambda _: True
    if cleanup_error:
        def stop(_):
            raise Conflict(cleanup_error)

        monkeypatch.setattr(release.Runtime, "stop", stop)
    with pytest.raises(Conflict, match="release_health_mismatch"):
        release.manager.deploy(release.path)
    assert release.live.starts == 1
    with release.store.connection() as db:
        assert bool(release.store.draining(db)) == bool(cleanup_error)
    assert bool(read_json(release.root / "pending.json")) == bool(cleanup_error)
    status = read_json(release.root / "status.json")
    assert status["state"] == "blocked"
    if cleanup_error:
        assert release.live.alive == release.manifest.id
        assert status["reason"] == cleanup_error
    else:
        assert release.live.stops == 1 and release.live.alive is None
        assert read_json(release.root / "selection.json") == {}


def test_health_rejects_http_200_wrong_version_assets_and_unprotected_api(release, monkeypatch):
    manifest = release.manifest
    record = {**process_identity(os.getpid()), "token": "private", "release_id": manifest.id}
    atomic_json(release.root / "process.json", record)
    actual = {"id": manifest.id, "commit": manifest.commit, "code_root": manifest.code_root,
              "python": manifest.python,
              "frontend": recovery.digest(asset_files(Path(manifest.code_root))),
              "database": manifest.database, "database_id": manifest.database_id,
              "config_digest": manifest.config_digest, "pid": os.getpid(),
              "ready": True, "monitoring": False}
    bad = {"version": True, "asset": False}

    def handle(request):
        if request.url.path == "/api/release-health":
            return httpx.Response(
                200, json={**actual, "commit": "f" * 40} if bad["version"] else actual)
        name = "index.html" if request.url.path == "/" else request.url.path[1:]
        return httpx.Response(200, content=b"wrong" if bad["asset"] else
                              (Path(manifest.code_root) / "dist" / name).read_bytes())

    client = httpx.Client
    monkeypatch.setattr("repowatch.release_runtime.httpx.Client",
                        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)))
    runtime = NativeRuntime(manifest, release.path)
    with pytest.raises(Conflict, match="health_mismatch"):
        runtime.health()
    bad.update(version=False, asset=True)
    with pytest.raises(Conflict, match="served_frontend_mismatch"):
        runtime.health()
    bad["asset"] = False
    assert runtime.health()["ready"]
    monkeypatch.undo()
    app = create_app(release.store.path, start_worker=False, credential_vault=MemoryVault())
    with TestClient(app, base_url="http://127.0.0.1") as api:
        assert api.get("/api/system/releases").status_code == 401
        assert api.get("/api/release-health").status_code == 404
        app.state.store.setup_password("isolated-password")
        session = api.post("/api/login", json={"password": "isolated-password"}).json()
        assert api.get("/api/system/releases").json()["managed"] is False
        with release.store.connection() as db:
            db.execute("UPDATE release_control SET draining='fixture' WHERE id=1")
        assert app.state.execution.claim() is None
        assert app.state.maintenance.claim() is None
        response = api.post("/api/repositories", json={"name": "owner/blocked", "url": "x"},
                            headers={"X-CSRF-Token": session["csrf"]})
        assert response.status_code == 503


def test_supervisor_restart_budget_survives_restart_and_protects_processes(release, monkeypatch):
    atomic_json(release.root / "selection.json", {"current": str(release.path), "previous": None})
    for _ in range(3):
        release.live.alive = None
        Supervisor(release.root, runtime_factory=release.Runtime).tick()
    release.live.alive = None
    Supervisor(release.root, runtime_factory=release.Runtime).tick()
    assert release.live.starts == 3
    assert read_json(release.root / "status.json")["reason"] == "release_restart_limit"
    wrong = {**process_identity(os.getpid()), "created": 0}
    with pytest.raises(Conflict, match="process_identity_changed"):
        matching_process(wrong)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", release.manifest.port))
        Supervisor(release.root, runtime_factory=release.Runtime).tick()
    assert release.live.starts == 3 and release.live.stops == 0
    assert read_json(release.root / "status.json")["reason"] == "release_port_conflict"
    atomic_json(release.root / "process.json",
                {"release_id": release.manifest.id, "state": "starting", "token": "fixture"})
    monkeypatch.setattr("repowatch.release_runtime.psutil.process_iter", lambda *_: [])
    NativeRuntime(release.manifest, release.path).stop()
    assert not read_json(release.root / "process.json")


def test_offline_journal_delivery_is_idempotent_and_keeps_failure_history(release, monkeypatch):
    journal = Journal(release.root)
    journal.event(release.manifest.id, "restart", "release_process_not_running",
                  event_key="stable-restart", commit=release.manifest.commit)
    original = release.store.connection

    def offline():
        raise sqlite3.OperationalError("offline")

    monkeypatch.setattr(release.store, "connection", offline)
    with pytest.raises(sqlite3.OperationalError):
        journal.deliver(release.store)
    monkeypatch.setattr(release.store, "connection", original)
    Journal(release.root).deliver(release.store)
    journal.event(release.manifest.id, "restart", "release_process_not_running",
                  event_key="stable-restart", commit=release.manifest.commit)
    journal.deliver(release.store)
    SystemFaults(release.store).tick()
    with release.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM release_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM system_fault_occurrences "
                          "WHERE source_event_key='supervisor:stable-restart'").fetchone()[0] == 1


def test_rollback_keeps_new_writes_and_interrupted_release_can_reconcile(release):
    release.manager.deploy(release.path)
    candidate = altered(release.manifest, pr=2, previous=str(release.path))
    path = save(candidate)
    grant(release.store, candidate)

    def failed_health(current):
        if current.id == candidate.id:
            with release.store.connection() as db:
                db.execute("INSERT OR IGNORE INTO settings VALUES('post_release_write','retained')")
            return True
        return False

    release.live.failure = failed_health
    with pytest.raises(Conflict, match="health_mismatch"):
        release.manager.deploy(path)
    assert read_json(release.root / "selection.json")["current"] == str(release.path)
    assert release.store.setting("post_release_write") == "retained"
    with release.store.connection() as db:
        db.execute("UPDATE release_control SET draining=? WHERE id=1", (candidate.id,))
    atomic_json(release.root / "pending.json",
                {"candidate": str(path), "previous": str(release.path), "stage": "draining"})
    assert release.manager.reconcile(path)["state"] == "prepared_cancelled"
    active, _, _ = analysis(release.store, "running")
    release.live.alive = None
    stops = release.live.stops
    atomic_json(release.root / "pending.json",
                {"candidate": str(path), "previous": str(release.path), "stage": "draining"})
    with pytest.raises(Conflict, match="active_tasks"):
        release.manager.reconcile(path)
    assert release.live.stops == stops
    with release.store.connection() as db:
        db.execute("UPDATE repository_analyses SET status='failed' WHERE id=?", (active,))
    release.live.alive = release.manifest.id
    assert release.manager.reconcile(path)["state"] == "prepared_cancelled"
    atomic_json(release.root / "pending.json",
                {"candidate": str(path), "previous": str(release.path), "stage": "completed",
                 "result": "prepared_cancelled"})
    starts, stops = release.live.starts, release.live.stops
    with release.store.connection() as db:
        db.execute("UPDATE release_control SET draining=? WHERE id=1", (candidate.id,))
    assert release.manager.reconcile(path)["state"] == "prepared_cancelled"
    assert (release.live.starts, release.live.stops) == (starts, stops)
    with release.store.connection() as db:
        assert not release.store.draining(db)
        db.execute("ALTER TABLE settings ADD COLUMN incompatible INTEGER")
    with pytest.raises(Conflict, match="rollback_schema_changed"):
        release.manifest.validate_files(rollback=True)
    assert release.store.setting("post_release_write") == "retained"


@pytest.fixture
def resumption(release, monkeypatch):
    store = release.store
    source_id, _, _ = analysis(store)
    now = time.time()
    attempt(store, source_id)
    failure = source_event(store, source_id, "failure",
                           {"code": "analysis_scope_read_not_followed"})
    SystemFaults(store).tick()
    with store.connection() as db:
        occurrence = db.execute("SELECT id FROM system_fault_occurrences "
                                "WHERE source_event_key=?",
                                (f"analysis_events:{failure}",)).fetchone()[0]
        db.execute("UPDATE system_fault_occurrences SET state='blocked' WHERE id=?", (occurrence,))
        db.execute("UPDATE maintenance_configuration SET enabled=1 WHERE id=1")
        db.execute("INSERT INTO recovery_roots(id,requests,seconds) VALUES(?,2,10)", (source_id,))
        db.execute("INSERT INTO analysis_report_recovery(analysis_id,state,started,deadline,"
                   "requests,stop_reason) VALUES(?,'blocked',?,?,2,'non_recoverable')",
                   (source_id, now, now + 1800))
        db.execute("INSERT INTO maintenance_tasks(id,occurrence_id,root_id,state,"
                   "configuration_version,basis,binding,owner,lease,action_id,code,remote,"
                   "created,updated) VALUES('maintenance_fixture',?,?,'waiting_deployment',"
                   "1,'bound',?,'owner',?,'old-action','internal',?, ?,?)",
                   (occurrence, source_id, json.dumps(BINDING), now + 1800,
                    json.dumps({"number": 1}), now, now))
    manifest = altered(release.manifest, maintenance_task="maintenance_fixture")
    grant(store, manifest, resume=True)
    Journal(release.root).event(manifest.id, "deployed", event_key=manifest.id + ":deployed")
    Journal(release.root).deliver(store)
    maintenance = SimpleNamespace(
        matches_basis=lambda _db, _occurrence, expected: expected == "bound")
    analyses = Analyses(SimpleNamespace(store=store),
                        SimpleNamespace(status=lambda: {"enabled": True}))
    service = Releases(store, maintenance, analyses, None, None, manifest)
    monkeypatch.setattr(NativeRuntime, "health", lambda _: {"ready": True})
    service.resume_original()
    service.resume_original()
    return service, source_id, occurrence


def test_release_resumes_only_real_original_adapter_with_budget_and_authorization(
    release, resumption,
):
    service, source_id, occurrence = resumption
    store, manifest = release.store, service.manifest
    with store.connection() as db:
        row = db.execute("SELECT * FROM release_resumptions").fetchone()
        assert row["state"] == "verifying_original", row["reason"]
        assert db.execute("SELECT status FROM repository_analyses WHERE id=?",
                          (source_id,)).fetchone()[0] == "queued"
        assert db.execute("SELECT requests FROM recovery_roots WHERE id=?",
                          (source_id,)).fetchone()[0] == 2
        assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                          (occurrence,)).fetchone()[0] != "resolved"
        db.execute("UPDATE release_authorizations SET revoked=1 WHERE target=?", (manifest.id,))
        with pytest.raises(Conflict, match="authorization_revoked"):
            require_source_authorization(db, "analysis", source_id)
        db.execute("UPDATE recovery_roots SET requests=? WHERE id=?",
                   (1000, source_id))
        db.execute("UPDATE repository_analyses SET status='failed' WHERE id=?", (source_id,))
    service.reconcile_resumptions()
    with store.connection() as db:
        row = db.execute("SELECT * FROM release_resumptions").fetchone()
        assert row["state"] == "blocked"
        assert row["reason"] == "release_resume_authorization_revoked"
        with pytest.raises(Conflict, match="authorization_revoked"):
            require_source_authorization(db, "analysis", source_id)
    exhausted = altered(manifest, pr=2)
    grant(store, exhausted, resume=True)
    Journal(release.root).event(exhausted.id, "deployed")
    Journal(release.root).deliver(store)
    with store.connection() as db:
        db.execute("UPDATE maintenance_tasks SET remote=?", (json.dumps({"number": 2}),))
    Releases(store, service.maintenance, service.analyses, None, None, exhausted).resume_original()
    with store.connection() as db:
        assert db.execute("SELECT reason FROM release_resumptions WHERE release_id=?",
                          (exhausted.id,)).fetchone()[0] != "release_original_budget_exhausted"


@pytest.mark.parametrize("stop", ["failed", "cancelled", "stale", "account", "budget",
                                  "deadline", "timeout"])
def test_original_verification_stops_without_replaying_or_resetting(release, resumption, stop):
    service, source_id, occurrence = resumption
    reasons = {"account": "release_account_changed", "budget": "release_original_budget_exhausted",
               "deadline": "coordination_source_budget_exhausted",
               "timeout": "release_source_verification_timeout"}
    with release.store.connection() as db:
        if stop in {"failed", "cancelled", "stale"}:
            db.execute("UPDATE repository_analyses SET status=? WHERE id=?", (stop, source_id))
        elif stop == "account":
            db.execute("UPDATE github_accounts SET version=version+1")
        elif stop == "budget":
            db.execute("UPDATE recovery_roots SET requests=? WHERE id=?",
                       (1000, source_id))
        elif stop == "deadline":
            db.execute("UPDATE analysis_report_recovery SET deadline=0 WHERE analysis_id=?",
                       (source_id,))
        else:
            db.execute("UPDATE release_resumptions SET created=?",
                       (time.time() - 10000 - 1,))
        before = db.execute("SELECT version FROM repository_analyses WHERE id=?",
                            (source_id,)).fetchone()[0]
    service.reconcile_resumptions()
    service.resume_original()
    service.reconcile_resumptions()
    with release.store.connection() as db:
        row = db.execute("SELECT * FROM release_resumptions").fetchone()
        if stop in {"budget", "deadline", "timeout"}:
            assert row["state"] == "verifying_original"
        else:
            assert (row["state"], row["reason"]) == (
                "blocked", reasons.get(stop, "release_source_" + stop))
        assert db.execute("SELECT version FROM repository_analyses WHERE id=?",
                          (source_id,)).fetchone()[0] == before
        assert db.execute("SELECT requests FROM recovery_roots WHERE id=?",
                          (source_id,)).fetchone()[0] == (1000 if stop == "budget"
                                                         else 2)
        assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                          (occurrence,)).fetchone()[0] != "resolved"


@pytest.mark.parametrize("verification", ["original", "released", "wrong_version", "wrong_model",
                                          "revoked"])
def test_original_completion_waits_for_verified_event_and_accepts_delayed_collection(
    release, resumption, monkeypatch, verification,
):
    service, source_id, occurrence = resumption
    with release.store.connection() as db:
        db.execute("UPDATE repository_analyses SET status='running' WHERE id=?", (source_id,))
        db.execute("UPDATE recovery_roots SET requests=? WHERE id=?",
                   (1000, source_id))
    service.reconcile_resumptions()
    with release.store.connection() as db:
        assert db.execute("SELECT state FROM release_resumptions").fetchone()[0] == (
            "verifying_original")
        db.execute("UPDATE repository_analyses SET status='completed' WHERE id=?", (source_id,))
    service.reconcile_resumptions()
    with release.store.connection() as db:
        assert db.execute("SELECT state FROM release_resumptions").fetchone()[0] == (
            "verifying_original")
    version = ("build1" if verification == "original" else "unrelated-build"
               if verification == "wrong_version" else release.store.workbench_version)
    attempt(release.store, source_id, 2, workbench_version=version)
    if verification == "wrong_model":
        source_event(release.store, source_id, "model_configuration",
                     {"role": "repository_analyst", "model": "unrelated-model",
                      "model_version": 99})
    source_event(release.store, source_id, "fault_verified", "analysis_report_validated")
    now = time.time()
    monkeypatch.setattr("repowatch.releases.time.time", lambda: now + 10000 + 1)
    service.reconcile_resumptions()
    with release.store.connection() as db:
        assert db.execute("SELECT state FROM release_resumptions").fetchone()[0] == (
            "verifying_original")
    if verification == "revoked":
        replacement = altered(service.manifest, pr=2)
        grant(release.store, replacement, resume=True)
        with release.store.connection() as db:
            db.execute("UPDATE release_authorizations SET revoked=1 WHERE target=?",
                       (service.manifest.id,))
            db.execute("INSERT INTO release_resumptions VALUES(?,?,'verifying_original',NULL,?)",
                       (replacement.id, service.manifest.maintenance_task, time.time()))
            require_source_authorization(db, "analysis", source_id)
            with pytest.raises(Conflict, match="authorization_revoked"):
                require_source_authorization(db, "analysis", source_id,
                                             release_id=service.manifest.id)
    SystemFaults(release.store).tick()
    service.reconcile_resumptions()
    with release.store.connection() as db:
        row = db.execute("SELECT * FROM release_resumptions WHERE release_id=?",
                         (service.manifest.id,)).fetchone()
        if verification in {"original", "released"}:
            assert (row["state"], row["reason"]) == ("completed", "source_verified")
            assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                              (occurrence,)).fetchone()[0] == "resolved"
        else:
            assert row["state"] == "blocked"
            assert db.execute("SELECT state FROM system_fault_occurrences WHERE id=?",
                              (occurrence,)).fetchone()[0] != "resolved"
