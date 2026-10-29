"""Durable policy, fencing and scope batches; no live models or remote writes."""

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_analysis_tool_arguments import ITEM, response
from test_execution import service  # noqa: F401
from test_repairs import confirm, enable, fixture_patch, pending  # noqa: F401
from test_repository_analysis import Engine, ReviewModel, prepare_tested

from repowatch import recovery
from repowatch.app import create_app
from repowatch.repair_workspace import Patch
from repowatch.repository_analysis import Analyses, run_analysis
from repowatch.store import Conflict, Store
from repowatch.task_scopes import register_scope_batch, scope_packet


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "recovery.db")
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def claim(db, flow="analysis", task="root", attempt=1, owner="one", **kwargs):
    return recovery.claim(
        db,
        flow,
        {"id": task, "analysis_id": "root"},
        attempt,
        ["revision", "model", "account", "scope"],
        owner,
        time.time() + 1000,
        **kwargs,
    )


def test_cross_flow_ownership_and_stable_receipts(db):
    first = claim(db)
    assert first and claim(db, "repair", "child") is None
    recovery.spend(db, first, "one", "request-1", "requests")
    recovery.spend(db, first, "one", "request-1", "requests")
    recovery.spend(db, first, "one", "usage-1", "tokens", 27)
    recovery.finish(db, first, "one", "failed", "analysis_provider_timeout")
    assert claim(db) is None
    second = claim(db, "repair", "child", owner="two")
    recovery.spend(db, second, "two", "request-1", "requests")
    state = recovery.snapshot(db, "root")
    assert state["budget"]["requests"] == 2 and state["budget"]["tokens"] == 27
    assert state["budget"]["cost"] is None
    assert json.loads(next(a["receipt"] for a in state["actions"] if a["id"] == first)) == {
        "source_state": "failed",
        "reason": "analysis_provider_timeout",
    }
    with pytest.raises(Conflict, match="ownership_changed"):
        recovery.spend(db, first, "one", "stale", "requests")


def test_two_connections_compete_in_business_transaction(tmp_path):
    store = Store(tmp_path / "race.db")

    def worker(owner):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return claim(db, owner=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["old-worker", "new-worker"]))
    assert sum(bool(result) for result in results) == 1
    with Store(store.path).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 1
        assert claim(db, owner="restarted") is None


def test_expired_worker_is_fenced_and_elapsed_is_retained(db, monkeypatch):
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    first = recovery.claim(db, "analysis", {"id": "root"}, 1, "basis", "old", 1010)
    monkeypatch.setattr(recovery.time, "time", lambda: 1011)
    second = recovery.claim(db, "analysis", {"id": "root"}, 2, "basis", "new", 1100)
    assert first != second
    assert recovery.snapshot(db, "root")["budget"]["seconds"] == 10
    assert recovery.snapshot(db, "root")["budget"]["resumptions"] == 1
    recovery.finish(db, first, "old", "completed")
    assert recovery.active(db, second, "new")
    assert (
        db.execute("SELECT state FROM recovery_actions WHERE id=?", (first,)).fetchone()[0]
        == "interrupted"
    )


@pytest.mark.parametrize(
    "metric,amount",
    [
        ("requests", recovery.MAX_REQUESTS),
        ("tokens", recovery.MAX_TOKENS),
        ("seconds", recovery.MAX_SECONDS),
    ],
)
def test_root_budget_survives_role_and_child_changes(db, metric, amount):
    first = claim(db)
    recovery.spend(db, first, "one", "consume", metric, amount)
    recovery.finish(db, first, "one", "failed")
    second = claim(db, "acceptance", "followup", owner="two")
    with pytest.raises(Conflict, match="budget_exhausted"):
        recovery.spend(db, second, "two", "new-request", "requests")
    assert recovery.snapshot(db, "root")["budget"][metric] >= amount


def test_upgrade_seeds_known_historical_requests_once(db):
    db.executescript("""
        INSERT INTO analysis_report_recovery
        (analysis_id,state,started,deadline,requests,resumptions,retry_budget)
        VALUES('root','blocked',0,1000,9,2,'{"used":2}');
    """)
    first = claim(db)
    assert recovery.snapshot(db, "root")["budget"]["requests"] == 9
    recovery.finish(db, first, "one", "failed")
    recovery.initialize(db)
    claim(db, attempt=2)
    assert recovery.snapshot(db, "root")["budget"]["requests"] == 9
    assert recovery.snapshot(db, "root")["budget"]["corrections"] == 2


def test_root_elapsed_guard_stops_tools_not_only_model_requests(db, monkeypatch):
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    action = claim(db)
    db.execute("UPDATE recovery_roots SET seconds=?", (recovery.MAX_SECONDS - 1,))
    recovery.require(db, action, "one")
    monkeypatch.setattr(recovery.time, "time", lambda: 1002)
    with pytest.raises(Conflict, match="budget_exhausted"):
        recovery.require(db, action, "one")


def test_stopped_and_fenced_actions_still_account_late_usage_once(db):
    action = claim(db)
    recovery.observe(db, action, "one", "request_started")
    db.execute("UPDATE recovery_roots SET seconds=?", (recovery.MAX_SECONDS,))
    recovery.observe(db, action, "one", "usage", 50)
    recovery.finish(db, action, "one", "failed")
    recovery.observe(db, action, "one", "usage", 50)
    assert recovery.snapshot(db, "root")["budget"]["tokens"] == 50
    with pytest.raises(Conflict, match="ownership_changed"):
        recovery.require(db, action, "one")


def failed_repair(fixture):
    repairs, *_ = fixture
    task = confirm(fixture)
    enable(repairs)
    with repairs.store.connection() as db:
        recovery.configure(db, True, 1)
        db.execute("UPDATE recovery_configuration SET enabled_since=?", (time.time() - 200,))
    running = repairs.claim()
    with repairs.store.connection() as db:
        db.execute(
            "UPDATE repair_tasks SET status='failed',error='repair_model_timeout',"
            "updated=? WHERE id=?",
            (time.time() - 100, task["id"]),
        )
    recovery.release(
        repairs.store, running["_recovery_action"], repairs.owner, "repair_tasks", task["id"]
    )
    return repairs, task


@pytest.mark.parametrize("change", ["model", "account", "scope", "disabled"])
def test_queued_automatic_retry_rechecks_immutable_basis(pending, monkeypatch, change):  # noqa: F811
    repairs, task = failed_repair(pending)
    repairs._queue_automatic_recovery()
    assert repairs.get(task["id"])["status"] == "queued"
    if change == "model":
        monkeypatch.setattr(
            repairs.engine,
            "status",
            lambda: {
                "enabled": True,
                "role_models": {"repair_agent": {"version": 99}},
            },
        )
    else:
        with repairs.store.connection() as db:
            if change == "account":
                db.execute("UPDATE github_accounts SET version=version+1")
            elif change == "scope":
                db.execute("UPDATE task_scopes SET version=version+1")
            else:
                recovery.configure(db, False, 2)
    assert repairs.claim() is None
    assert repairs.get(task["id"])["error"] == (
        "recovery_disabled" if change == "disabled" else "repair_basis_changed"
    )
    assert repairs.engine.models == []


def test_crashed_repair_settles_unknown_receipt_then_resumes(pending):  # noqa: F811
    repairs, *_ = pending
    task = confirm(pending)
    enable(repairs)
    with repairs.store.connection() as db:
        recovery.configure(db, True, 1)
        db.execute("UPDATE recovery_configuration SET enabled_since=?", (time.time()-200,))
    running = repairs.claim()
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET lease=? WHERE id=?", (time.time() - 1, task["id"]))
        db.execute(
            "UPDATE recovery_roots SET lease=? WHERE id=?",
            (time.time() - 1, running["analysis_id"]),
        )
    assert repairs.claim() is None
    with repairs.store.connection() as db:
        receipt = db.execute(
            "SELECT * FROM recovery_actions WHERE id=?", (running["_recovery_action"],)
        ).fetchone()
        assert receipt["state"] == "interrupted"
        assert json.loads(receipt["receipt"])["execution"] == "unknown"
        db.execute("UPDATE repair_tasks SET updated=? WHERE id=?", (time.time() - 100, task["id"]))
    repairs._queue_automatic_recovery()
    assert repairs.get(task["id"])["status"] == "queued"
    assert repairs.claim()["attempt"] == 2


@pytest.mark.parametrize("exhausted", ["resumptions", "seconds", "stop_reason"])
def test_real_repair_resume_exhaustion_is_failed_not_stuck_queued(pending, exhausted):  # noqa: F811
    repairs, task = failed_repair(pending)
    with repairs.store.connection() as db:
        value = {
            "resumptions": recovery.MAX_RESUMPTIONS,
            "seconds": recovery.MAX_SECONDS,
            "stop_reason": "recovery_budget_exhausted",
        }[exhausted]
        db.execute(
            f"UPDATE recovery_roots SET {exhausted}=? WHERE id=?", (value, task["analysis_id"])
        )
    repairs.retry(task["id"], repairs.get(task["id"])["version"])
    assert asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "failed" and result["error"] == "recovery_budget_exhausted"
    assert not asyncio.run(repairs.tick())


def test_stale_repair_artifacts_cannot_overwrite_new_attempt(pending):  # noqa: F811
    repairs, task = failed_repair(pending)
    repairs.retry(task["id"], repairs.get(task["id"])["version"])
    running = repairs.claim()
    with repairs.store.connection() as db:
        recovery.finish(db, running["_recovery_action"], repairs.owner, "interrupted")
        db.execute(
            "UPDATE repair_tasks SET attempt=attempt+1,patch='{}',diff='new' WHERE id=?",
            (task["id"],),
        )
    workspace = SimpleNamespace(
        patch=Patch.model_validate(fixture_patch()),
        original=repairs.execution.directory(running["env_id"]) / "source",
        diff="stale",
        review=None,
        verification={},
        verification_history=[],
        review_history=[],
    )
    repairs._save_artifacts(
        task["id"], workspace, action=running["_recovery_action"], attempt=running["attempt"]
    )
    with repairs.store.connection() as db:
        saved = db.execute(
            "SELECT diff,patch FROM repair_tasks WHERE id=?", (task["id"],)
        ).fetchone()
        assert tuple(saved) == ("new", "{}")


def test_automatic_candidate_scan_does_not_starve_new_failure(pending):  # noqa: F811
    repairs, task = failed_repair(pending)
    with repairs.store.connection() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(repair_tasks)")]
        original = dict(
            db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        )
        for index in range(32):
            finding = f"historical-finding-{index}"
            db.execute(
                "INSERT INTO analysis_findings(id,analysis_id,data) VALUES(?,?,'{}')",
                (finding, task["analysis_id"]),
            )
            historical = {
                **original,
                "id": f"history-{index}",
                "finding_id": finding,
                "updated": time.time() - 1000,
            }
            db.execute(
                f"INSERT INTO repair_tasks({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                [historical[key] for key in columns],
            )
    repairs._queue_automatic_recovery()
    assert repairs.get(task["id"])["status"] == "queued"


def test_automatic_policy_default_off_history_binding_and_switch_fencing(db, monkeypatch):
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    assert recovery.state(db)["enabled"] is False
    action = claim(db, "repair", "child")
    recovery.finish(db, action, "one", "failed")
    monkeypatch.setattr(recovery.time, "time", lambda: 1001)
    enabled = recovery.configure(db, True, 1)
    task = {
        "id": "child",
        "analysis_id": "root",
        "version": 3,
        "updated": 1000,
        "error": "repair_model_timeout",
        "context": '{"_recovery_binding":"binding"}',
    }
    with pytest.raises(Conflict, match="automatic_unavailable"):
        recovery.authorize_automatic(db, task, "binding", now=1050)
    monkeypatch.setattr(recovery.time, "time", lambda: 1010)
    action = claim(db, "repair", "child", attempt=2)
    recovery.finish(db, action, "one", "failed")
    task["updated"] = 1010
    with pytest.raises(Conflict, match="automatic_unavailable"):
        recovery.authorize_automatic(db, task, "changed-model", now=1030)
    with pytest.raises(Conflict, match="automatic_unavailable"):
        recovery.authorize_automatic(db, task, "binding", now=1011)
    assert recovery.authorize_automatic(db, task, "binding", now=1030) == enabled["version"]
    with pytest.raises(Conflict, match="action_exists"):
        recovery.authorize_automatic(db, task, "binding", now=1050)
    recovery.configure(db, False, enabled["version"])
    with pytest.raises(Conflict, match="recovery_disabled"):
        recovery.automatic_guard(db, {"_automatic_recovery_version": enabled["version"]})
    assert recovery.snapshot(db, "root")["budget"]["resumptions"] == 1


@pytest.fixture
def scope_service(service):  # noqa: F811
    execution, _, _ = service
    analysis = Analyses(execution, Engine())
    identity = analysis.enqueue(prepare_tested(service)["id"])
    with execution.store.connection() as db:
        db.execute(
            "UPDATE repository_analyses SET status='running',context=? WHERE id=?",
            (json.dumps({"files": {"app.py": "print('ok')"}}), identity),
        )
    return execution.store, identity


@pytest.mark.parametrize("invalid", ["path", "conflict", "count", "empty"])
def test_scope_batch_rejects_every_item_atomically(scope_service, invalid):
    store, identity = scope_service
    batches = [[ITEM], [{**ITEM, "key": "second"}]]
    if invalid == "path":
        batches[1][0]["paths"] = ["missing.py"]
    elif invalid == "conflict":
        batches[1][0].update(key=ITEM["key"], expected="Different behavior")
    elif invalid == "count":
        batches.append([ITEM])
    else:
        batches[1] = []
    with store.connection() as db:
        row = db.execute("SELECT * FROM repository_analyses WHERE id=?", (identity,)).fetchone()
        before = scope_packet(db, identity)
        with pytest.raises((Conflict, ValueError)):
            register_scope_batch(db, row, batches, "repository_analyst", "batch", before["version"])
        assert scope_packet(db, identity) == before
        assert db.execute("SELECT COUNT(*) FROM task_scope_batches").fetchone()[0] == 0


def test_scope_order_receipts_replay_prevalidation_and_version_race(scope_service):
    store, identity = scope_service
    batches = [[ITEM], [{**ITEM, "key": "second"}]]
    with store.connection() as db:
        row = db.execute("SELECT * FROM repository_analyses WHERE id=?", (identity,)).fetchone()
        before = scope_packet(db, identity)
        args = (db, row, batches, "repository_analyst", "batch", before["version"])
        previews = register_scope_batch(*args, validate=True)
        assert scope_packet(db, identity) == before
        receipts = register_scope_batch(*args)
        assert previews == receipts
        assert len(receipts[0]["items"]) == 1 and len(receipts[1]["items"]) == 2
        assert receipts[0]["version"] + 1 == receipts[1]["version"]
        assert register_scope_batch(*args) == receipts
        with pytest.raises(Conflict, match="scope_changed"):
            register_scope_batch(
                db,
                row,
                [[{**ITEM, "key": "third"}]],
                "repository_analyst",
                "other",
                before["version"],
            )
        assert scope_packet(db, identity) == receipts[-1]


def test_actual_sdk_executes_two_scope_calls_once_with_real_receipts(scope_service):
    store, identity = scope_service
    events = []

    class TwoScopes(ReviewModel):
        async def get_response(self, *args, **kwargs):
            if kwargs["model_settings"].tool_choice == "register_task_scope":
                return response(
                    ("register_task_scope", {"items": [ITEM]}),
                    ("register_task_scope", {"items": [{**ITEM, "key": "second"}]}),
                )
            return await super().get_response(*args, **kwargs)

    def event(kind, value):
        events.append(kind)
        if kind in ("scope_batch_check", "scope_batch_execute"):
            packet = json.loads(value)
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM repository_analyses WHERE id=?", (identity,)
                ).fetchone()
                return register_scope_batch(
                    db,
                    row,
                    packet["batches"],
                    "repository_analyst",
                    packet["id"],
                    packet["version"],
                    validate=kind == "scope_batch_check",
                )

    with store.connection() as db:
        scope = scope_packet(db, identity)
    context = {
        "files": {"app.py": "print('ok')"},
        "config": {},
        "jobs": [],
        "test_result": None,
        "task_scope": scope,
    }
    result = asyncio.run(run_analysis(TwoScopes(), context, event))
    assert len(result["handoffs"]) == 3
    assert {i["key"] for i in context["task_scope"]["items"]} == {"behavior", "second"}
    assert events.count("scope_batch_execute") == 1 and events.count("scope_registered") == 2


def test_recovery_configuration_requires_login_csrf_and_version(tmp_path):
    with TestClient(
        create_app(tmp_path / "api.db", start_worker=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/system/recovery").status_code == 401
        session = client.post("/api/setup", json={"password": "strong-password"}).json()
        settings = client.get("/api/system/recovery").json()
        assert settings["enabled"] is False
        body = {"version": settings["version"], "enabled": True}
        assert client.post("/api/system/recovery/settings", json=body).status_code == 403
        headers = {"x-csrf-token": session["csrf"]}
        assert (
            client.post("/api/system/recovery/settings", json=body, headers=headers).status_code
            == 200
        )
        assert (
            client.post("/api/system/recovery/settings", json=body, headers=headers).status_code
            == 409
        )
