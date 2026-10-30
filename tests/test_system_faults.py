"""Phase 1 records facts without a model, runtime, GitHub call or live database."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from repowatch.app import create_app
from repowatch.fault_contract import FaultEvent, diagnostic
from repowatch.store import Conflict, Missing, Store
from repowatch.system_faults import SystemFaults, enqueue, record_worker_failure, transition


def seed(store, suffix="one", *, account_id=1):
    now = time.time()
    repo_id, env_id, analysis_id = (f"{prefix}_{suffix}" for prefix in ("repo", "env", "analysis"))
    with store.connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO github_accounts"
            "(id,login,encrypted_token,state,checked_at,updated) "
            "VALUES(?,?,'unusable','verified',?,?)",
            (account_id, f"owner{account_id}", now, now),
        )
        db.execute("UPDATE github_configuration SET active_account_id=?", (account_id,))
        db.execute(
            "INSERT INTO repositories(id,name,url,kind,language,created) "
            "VALUES(?,?,?,'github','Python',?)",
            (repo_id, f"owner/{suffix}", f"https://github.com/owner/{suffix}", now),
        )
        db.execute(
            "INSERT INTO environments(id,repo_id,account_id,account_version,revision,"
            "config,state,created,updated) VALUES(?,?,?,1,?,'{}','ready',?,?)",
            (env_id, repo_id, account_id, "a" * 40, now, now),
        )
        db.execute(
            "INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,trigger_key,"
            "status,context,created,updated) VALUES(?,?,?,?,'basis','owner','running','{}',?,?)",
            (analysis_id, env_id, repo_id, "a" * 40, now, now),
        )
    return repo_id, env_id, analysis_id


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / "faults.db")
    ids = seed(store)
    return store, SystemFaults(store), ids


def event(ids, key="one", **changes):
    repo_id, env_id, task_id = ids
    values = dict(
        source_event_key=f"analysis_events:{key}",
        flow="analysis",
        task_id=task_id,
        source_table="analysis_events",
        source_id=key,
        occurred=time.time(),
        root_task_id=task_id,
        repo_id=repo_id,
        env_id=env_id,
        account_id=1,
        attempt=1,
        role="repository_analyst",
        stage="repository_analyst",
        code="analysis_scope_read_not_followed",
        category="protocol",
        execution="not_executed",
        revision="a" * 40,
        workbench_version="version1",
        model_binding={"model": "model1", "model_version": 3},
        diagnostic={
            "required": "scope",
            "calls": 2,
            "matching": 2,
            "other": 0,
            "allowed": 1,
            "accepted": False,
            "reason": "too_many_calls",
        },
    )
    return FaultEvent(**(values | changes))


def publish(store, value):
    with store.connection() as db:
        enqueue(db, value)


def source_event(store, analysis_id, kind, value):
    with store.connection() as db:
        return db.execute(
            "INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
            (
                analysis_id,
                kind,
                value if isinstance(value, str) else json.dumps(value),
                time.time(),
            ),
        ).lastrowid


def attempt(store, analysis_id, number=1, *, workbench_version="build1"):
    source_event(
        store, analysis_id, "fault_attempt",
        {"attempt": number, "workbench_version": workbench_version},
    )
    source_event(store, analysis_id, "agent", "repository_analyst")
    source_event(
        store,
        analysis_id,
        "model_configuration",
        {
            "role": "repository_analyst",
            "model": "bound-model",
            "model_version": 9,
            "configuration_version": 11,
            "provider_id": "provider_1",
            "provider_version": 4,
            "request_timeout_seconds": 120,
        },
    )


def test_contract_and_outbox_are_atomic_and_replay_is_idempotent(ledger):
    store, faults, ids = ledger
    with pytest.raises(RuntimeError):
        with store.connection() as db:
            enqueue(db, event(ids))
            raise RuntimeError("rollback")
    assert faults.tick() == 0
    publish(store, event(ids))
    publish(store, event(ids))
    assert faults.tick() == 1
    group = faults.list()["items"][0]
    assert group["occurrences"] == 1
    record = faults.detail(group["id"])["items"][0]
    assert record["diagnostic"]["calls"] == record["diagnostic"]["matching"] == 2
    assert record["diagnostic"]["allowed"] == 1
    assert record["execution"] == "not_executed"
    assert record["trace_id"] is None
    with store.connection() as db:
        db.execute("UPDATE system_fault_outbox SET consumed=0")
    faults.tick()
    assert faults.list()["items"][0]["occurrences"] == 1
    assert faults.history(group["id"])["total"] == 1


def test_same_group_retains_separate_attempts_and_partial_resolution(ledger):
    store, faults, ids = ledger
    publish(store, event(ids, "first"))
    publish(store, event(ids, "second", attempt=2))
    faults.tick()
    group = faults.list()["items"][0]
    assert group["occurrences"] == group["remaining"] == 2
    first, second = faults.detail(group["id"])["items"]
    with store.connection() as db:
        row = db.execute(
            "SELECT * FROM system_fault_occurrences WHERE id=?", (first["id"],)
        ).fetchone()
        with pytest.raises(Conflict, match="verification_required"):
            transition(db, row, "resolved", actor="owner", reason="pretend", evidence="manual")
        transition(db, row, "resolved", actor="source", reason="validated", evidence="event:proof")
    result = faults.list()["items"][0]
    assert result["remaining"] == 1 and result["state"] == "pending"
    faults.dismiss(group["id"], second["id"], second["version"], "无需继续处理")
    result = faults.list()["items"][0]
    assert result["remaining"] == 0 and result["state"] == "dismissed"
    with store.connection() as db:
        assert db.execute("SELECT status FROM repository_analyses").fetchone()[0] == "running"


@pytest.mark.parametrize(
    "changed",
    [
        {"revision": "b" * 40},
        {"workbench_version": "version2"},
        {"model_binding": {"model": "model2", "model_version": 4}},
        {"workbench_version": None, "task_id": "analysis_other"},
    ],
)
def test_grouping_keeps_incompatible_bases_separate(ledger, changed):
    store, faults, ids = ledger
    publish(store, event(ids))
    publish(store, event(ids, "second", **changed))
    faults.tick()
    assert faults.list()["total"] == 2


def test_scope_checks_list_detail_history_and_close_after_account_switch(ledger):
    store, faults, ids = ledger
    publish(store, event(ids))
    faults.tick()
    group = faults.list()["items"][0]
    occurrence = faults.detail(group["id"])["items"][0]
    seed(store, "other", account_id=2)
    assert faults.list()["total"] == 0
    for call in (
        lambda: faults.detail(group["id"]),
        lambda: faults.history(group["id"]),
        lambda: faults.dismiss(group["id"], occurrence["id"], 1, "hidden"),
    ):
        with pytest.raises(Missing):
            call()


def test_allowlist_discards_secrets_urls_bodies_and_bounds_lengths(ledger):
    store, faults, ids = ledger
    secret = "sk-PRIVATE-CREDENTIAL-123456"
    bad = {
        "api_key": secret,
        "Authorization": f"Bearer {secret}",
        "body": secret,
        "reasoning": secret,
        "raw_response": secret,
        "model": secret,
        "path": "https://user:password@example.com?token=" + secret,
        "errors": [{"path": "items[0].expected", "type": "missing", "input": secret}] * 100,
        "reason": "x" * 100000,
        "request_elapsed_seconds": float("nan"),
    }
    publish(store, event(ids, diagnostic=bad, model_binding=bad))
    faults.tick()
    group = faults.list()["items"][0]
    details = faults.detail(group["id"])
    assert details["items"][0]["diagnostic"]["truncated"]
    text = json.dumps(details)
    assert secret not in text and "user:password" not in text and "raw_response" not in text
    assert details["items"][0]["diagnostic"]["errors"][0] == {
        "path": "items[0].expected",
        "type": "missing",
    }
    with store.connection() as db:
        for table in ("system_fault_outbox", "system_fault_occurrences", "system_fault_history"):
            assert secret not in str([tuple(row) for row in db.execute(f"SELECT * FROM {table}")])
    assert diagnostic(
        {
            "output_validation": {
                "errors": [
                    {"path": ["items", 0, "expected"], "type": "string_type", "input": secret}
                ]
            }
        }
    )


def test_real_analysis_source_preserves_double_call_failure_without_duplicate_wrapper(ledger):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    source_event(
        store,
        task,
        "required_read_validation",
        {
            "required": "scope",
            "calls": 2,
            "matching": 2,
            "other": 0,
            "allowed": 1,
            "accepted": False,
            "reason": "too_many_calls",
            "recovery": "request_mismatch",
        },
    )
    source_event(
        store,
        task,
        "failure",
        {
            "code": "analysis_scope_read_not_followed",
            "stage": "repository_analyst",
            "elapsed_seconds": 90,
        },
    )
    faults.tick()
    assert faults.health()["error"] is None
    group = faults.list()["items"][0]
    assert group["occurrences"] == 1 and group["state"] == "blocked"
    record = faults.detail(group["id"])["items"][0]
    assert record["model_binding"]["model_version"] == 9
    assert record["model_binding"]["provider_version"] == 4
    assert record["execution"] == "not_executed"
    assert record["attempt"] == 1
    assert record["diagnostic"]["reason"] == "too_many_calls"


def test_model_binding_exposes_escalation_route_and_version(ledger):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    source_event(
        store,
        task,
        "model_configuration",
        {
            "role": "repository_analyst",
            "model": "bound-model",
            "model_version": 9,
            "configuration_version": 11,
            "provider_id": "provider_1",
            "provider_version": 4,
            "request_timeout_seconds": 120,
            "escalation_version": 7,
            "escalation_route": "monitor",
        },
    )
    source_event(
        store,
        task,
        "required_read_validation",
        {
            "required": "scope",
            "calls": 2,
            "matching": 2,
            "other": 0,
            "allowed": 1,
            "accepted": False,
            "reason": "too_many_calls",
        },
    )
    source_event(store, task, "failure", {"code": "analysis_scope_read_not_followed"})
    faults.tick()
    record = faults.detail(faults.list()["items"][0]["id"])["items"][0]
    assert record["model_binding"]["escalation_version"] == 7
    assert record["model_binding"]["escalation_route"] == "monitor"


def test_timeout_records_request_and_attempt_timing_and_bound_model(ledger):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    source_event(
        store,
        task,
        "model_request_failed",
        {
            "role": "repository_analyst",
            "request_timeout_seconds": 120,
            "request_elapsed_seconds": 120.25,
            "timing_scope": "request",
            "timeout_kind": "unknown",
        },
    )
    source_event(store, task, "timeout_retry", "repository_analyst")
    source_event(
        store,
        task,
        "failure",
        {
            "code": "analysis_provider_timeout_exhausted",
            "stage": "repository_analyst",
            "elapsed_seconds": 250,
        },
    )
    faults.tick()
    group = faults.list()["items"][0]
    assert group["occurrences"] == 1 and group["state"] == "blocked"
    record = faults.detail(group["id"])["items"][0]
    assert record["diagnostic"]["request_elapsed_seconds"] == 120.25
    assert record["diagnostic"]["timeout_kind"] == "unknown"
    assert record["execution"] == "unknown"
    history = faults.history(group["id"])["items"]
    assert history[0]["details"]["diagnostic"]["attempt_elapsed_seconds"] == 250
    assert "supplier_load" not in json.dumps(history)


def test_model_binding_never_falls_back_to_a_previous_attempt(ledger):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    source_event(store, task, "fault_attempt", {"attempt": 2, "workbench_version": "build1"})
    source_event(store, task, "failure", {"code": "new_error", "stage": "repository_analyst"})
    faults.tick()
    group = faults.list()["items"][0]
    assert faults.detail(group["id"])["items"][0]["model_binding"] == {}


def test_restart_consumes_pending_and_replays_persisted_source_after_capture_failure(
    ledger, monkeypatch
):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    original = __import__("repowatch.fault_sources", fromlist=["capture"]).capture

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("sk-do-not-leak")

    monkeypatch.setattr("repowatch.fault_sources.capture", broken)
    source_event(store, task, "failure", {"code": "analysis_provider_unreachable"})
    assert store.fault_capture_failures > 0
    assert faults.tick() == 0 and faults.health()["error"] == "OperationalError"
    with store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM analysis_events WHERE kind='failure'").fetchone()[0]
            == 1
        )
    monkeypatch.setattr("repowatch.fault_sources.capture", original)
    restarted = SystemFaults(Store(store.path))
    assert restarted.tick() > 0
    assert restarted.list()["total"] == 1
    restarted.tick()
    assert restarted.list()["items"][0]["occurrences"] == 1


def test_upgrade_does_not_import_history_or_change_task_status(ledger):
    store, _, (_, _, task) = ledger
    source_event(store, task, "failure", {"code": "analysis_provider_timeout"})
    # Simulate a pre-feature database by removing only the additive ledger tables.
    with sqlite3.connect(store.path) as db:
        for table in (
            "system_fault_history",
            "system_fault_occurrences",
            "system_faults",
            "system_fault_outbox",
            "system_fault_ingestion",
            "system_fault_configuration",
        ):
            db.execute(f"DROP TABLE {table}")
        before = db.execute("SELECT * FROM repository_analyses").fetchall()
    upgraded = Store(store.path)
    faults = SystemFaults(upgraded)
    faults.tick()
    assert faults.list()["total"] == 0
    faults.import_history()
    assert faults.list()["total"] == 1
    record = faults.detail(faults.list()["items"][0]["id"])["items"][0]
    assert record["historical"] is True and record["model_binding"] == {}
    faults.import_history()
    assert faults.list()["items"][0]["occurrences"] == 1
    Store(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM repository_analyses").fetchall() == before


@pytest.mark.parametrize(
    "code,category",
    [
        ("test_failed", "target_test"),
        ("docker_unavailable", "external_service"),
        ("unknown_code", "unknown"),
    ],
)
def test_execution_sources_preserve_target_test_vs_runtime_vs_unknown(ledger, code, category):
    store, faults, (_, env_id, _) = ledger
    now = time.time()
    with store.connection() as db:
        db.execute(
            "INSERT INTO execution_jobs(id,env_id,action,state,error,created,updated) "
            "VALUES('job_one',?,'test','failed',?,?,?)",
            (env_id, code, now, now),
        )
    faults.tick()
    group = faults.list()["items"][0]
    assert group["flow"] == "execution" and group["category"] == category


def test_service_fault_is_visible_without_a_repository_and_does_not_recurse(tmp_path):
    store = Store(tmp_path / "service.db")
    faults = SystemFaults(store)
    record_worker_failure(store, "analysis_worker")
    faults.tick()
    assert faults.list()["items"][0]["flow"] == "service"
    for _ in range(3):
        faults.tick()
    assert faults.list()["items"][0]["occurrences"] == 1


def test_api_login_csrf_bounds_scope_and_optimistic_close(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False)
    store, faults = app.state.store, app.state.faults
    ids = seed(store)
    publish(store, event(ids))
    faults.tick()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/system/faults").status_code == 401
        session = client.post("/api/setup", json={"password": "test-owner-password"}).json()
        headers = {"X-CSRF-Token": session["csrf"]}
        group = client.get("/api/system/faults").json()["items"][0]
        record = client.get(f"/api/system/faults/{group['id']}").json()["items"][0]
        close = f"/api/system/faults/{group['id']}/occurrences/{record['id']}/dismiss"
        assert (
            client.post(close, json={"version": 1, "reason": "owner decision"}).status_code == 403
        )
        assert client.get("/api/system/faults?limit=201").status_code == 422
        assert client.get(f"/api/system/faults/{group['id']}?limit=201").status_code == 422
        assert (
            client.post(close, headers=headers, json={"version": 99, "reason": "wrong"}).status_code
            == 409
        )
        assert (
            client.post(close, headers=headers, json={"version": 1, "reason": " "}).status_code
            == 409
        )
        assert (
            client.post(
                close, headers=headers, json={"version": 1, "reason": "done", "state": "resolved"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                close, headers=headers, json={"version": 1, "reason": "不需处理"}
            ).status_code
            == 200
        )
        assert (
            client.post(close, headers=headers, json={"version": 1, "reason": "stale"}).status_code
            == 409
        )
        assert client.get("/api/system/faults?state=dismissed").json()["total"] == 1
        assert client.get("/api/system/faults?state=resolved").json()["total"] == 0
        assert client.get(f"/api/system/faults/{group['id']}/history").json()["total"] == 2
        seed(store, "another", account_id=2)
        assert client.get(f"/api/system/faults/{group['id']}").status_code == 404


def seed_repair(store, ids):
    repo_id, env_id, analysis_id = ids
    with store.connection() as db:
        db.execute(
            "INSERT INTO analysis_findings(id,analysis_id,data) VALUES('finding_one',?,'{}')",
            (analysis_id,),
        )
        db.execute(
            "INSERT INTO repair_tasks(id,analysis_id,finding_id,env_id,repo_id,revision,"
            "stage,attempt,context,created,updated) VALUES('repair_one',?,'finding_one',?,?,?,"
            "'independent_reviewer',1,'{}',?,?)",
            (analysis_id, env_id, repo_id, "a" * 40, time.time(), time.time()),
        )


def repair_source(store, kind, value):
    with store.connection() as db:
        db.execute(
            "INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,?)",
            (
                "repair_one",
                kind,
                value if isinstance(value, str) else json.dumps(value),
                time.time(),
            ),
        )


def test_repair_review_rejection_and_native_verification_keep_history(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(store, "fault_attempt", {"attempt": 1, "workbench_version": "build1"})
    repair_source(store, "agent", "independent_reviewer")
    repair_source(
        store,
        "model_configuration",
        {"role": "independent_reviewer", "model": "bound", "model_version": 6},
    )
    repair_source(store, "review_feedback", {"approved": False, "body": "not copied"})
    faults.tick()
    group = faults.list(flow="repair")["items"][0]
    assert group["code"] == "repair_review_rejected"
    repair_source(store, "review_approved", "real tests and review passed")
    faults.tick()
    assert faults.list(flow="repair")["items"][0]["state"] == "resolved"
    assert faults.history(group["id"])["total"] == 2


def test_publication_failure_remains_unknown_even_if_task_stage_later_changes(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(store, "fault_attempt", {"attempt": 1, "workbench_version": "build1"})
    repair_source(store, "publishing", "")
    repair_source(store, "failure_details", {"code": "github_unreachable", "body": "not copied"})
    repair_source(store, "failure", "github_unreachable")
    with store.connection() as db:
        db.execute("UPDATE repair_tasks SET stage='pr_created' WHERE id='repair_one'")
    faults.tick()
    group = faults.list(flow="publication")["items"][0]
    assert group["state"] == "blocked" and group["occurrences"] == 1
    record = faults.detail(group["id"])["items"][0]
    assert record["execution"] == "unknown"
    assert record["root_task_id"] == ids[2]


def seed_acceptance(store, ids):
    with store.connection() as db:
        db.execute(
            "INSERT INTO task_scopes(id,repo_id,origin,analysis_id,revision,original,"
            "original_digest,status,created,updated) VALUES('scope_one',?,'owner',?,?,"
            "'{}','hash','open',?,?)",
            (ids[0], ids[2], "a" * 40, time.time(), time.time()),
        )
        db.execute(
            "INSERT INTO scope_acceptance_runs(id,scope_id,version,analysis_id,basis,basis_hash,"
            "state,attempt,model_calls,created,updated) "
            "VALUES('acceptance_one','scope_one',1,?,'{}',"
            "'hash','running',1,3,?,?)",
            (ids[2], time.time(), time.time()),
        )


def test_acceptance_failure_preserves_field_diagnostics_and_cumulative_budget(ledger):
    store, faults, ids = ledger
    seed_acceptance(store, ids)
    with store.connection() as db:
        db.execute(
            "INSERT INTO scope_acceptance_failures VALUES('failure_one','acceptance_one',"
            "'acceptance_output_invalid',?,0,?)",
            (
                json.dumps(
                    {
                        "fault_basis": {
                            "attempt": 1,
                            "workbench_version": "build1",
                            "budget": {"attempt": 1, "model_calls": 3, "max_model_calls": 12},
                        },
                        "output_validation": {
                            "category": "schema_mismatch",
                            "errors": [{"path": ["checks", 0, "files"], "type": "missing"}],
                        },
                    }
                ),
                time.time(),
            ),
        )
    faults.tick()
    group = faults.list(flow="acceptance")["items"][0]
    record = faults.detail(group["id"])["items"][0]
    assert record["attempt"] == 1 and record["budget"]["model_calls"] == 3
    assert record["budget"]["max_model_calls"] == 12
    assert record["diagnostic"]["output_validation"]["errors"][0]["path"] == ["checks", 0, "files"]


def test_real_acceptance_check_failure_is_target_behavior_and_not_a_system_code_defect(ledger):
    store, faults, ids = ledger
    seed_acceptance(store, ids)
    with store.connection() as db:
        db.execute(
            "INSERT INTO task_scope_events(scope_id,version,kind,actor,value,created) "
            "VALUES('scope_one',1,'acceptance_check_finished','system',?,?)",
            (
                json.dumps(
                    {
                        "run_id": "acceptance_one",
                        "attempt": 1,
                        "passed": False,
                        "executed": True,
                        "exit_code": 1,
                        "workbench_version": "build1",
                    }
                ),
                time.time(),
            ),
        )
    faults.tick()
    group = faults.list(flow="acceptance")["items"][0]
    assert group["category"] == "target_test"
    assert faults.detail(group["id"])["items"][0]["execution"] == "executed"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM repair_tasks").fetchone()[0] == 0


def test_diagnosis_failure_records_actual_role_and_safe_binding(ledger):
    store, faults, ids = ledger
    now = time.time()
    with store.connection() as db:
        db.execute(
            "INSERT INTO execution_jobs(id,env_id,action,state,created,updated) "
            "VALUES('job_diagnosis',?,'setup','failed',?,?)",
            (ids[1], now, now),
        )
        db.execute(
            "INSERT INTO execution_diagnoses(job_id,env_id,revision,fingerprint,context,report,"
            "state,created,updated,fault_role,fault_model_binding,fault_workbench_version) "
            "VALUES('job_diagnosis',?,?,'hash','{}',?,'failed',?,?,?,?,'build1')",
            (
                ids[1],
                "a" * 40,
                json.dumps({"model_status": "unavailable"}),
                now,
                now,
                "repository_analyst",
                json.dumps(
                    {"repository_analyst": {"model": "diagnosis-bound", "model_version": 5}}
                ),
            ),
        )
    faults.tick()
    group = faults.list(flow="diagnosis")["items"][0]
    record = faults.detail(group["id"])["items"][0]
    assert record["role"] == "repository_analyst"
    assert record["model_binding"]["model"] == "diagnosis-bound"


def test_recording_switch_only_controls_collection_and_preserves_cursors(ledger):
    store, faults, ids = ledger
    health = faults.configure(1, False)
    assert not health["enabled"]
    source_event(store, ids[2], "failure", {"code": "unknown_new_code"})
    assert faults.tick() == 0 and faults.list()["total"] == 0
    faults.configure(health["version"], True)
    faults.tick()
    assert faults.list()["total"] == 1
    with store.connection() as db:
        assert db.execute("SELECT status FROM repository_analyses").fetchone()[0] == "running"


def test_analysis_native_recovery_resolves_only_the_verified_binding(ledger):
    store, faults, (_, _, task) = ledger
    attempt(store, task)
    source_event(
        store,
        task,
        "failure",
        {"code": "analysis_provider_unreachable", "stage": "repository_analyst"},
    )
    attempt(store, task, 2)
    source_event(store, task, "fault_verified", "analysis_report_validated")
    faults.tick()
    assert faults.list()["items"][0]["state"] == "resolved"


def test_source_transaction_rolls_back_outbox_cursor_and_original_event(ledger):
    store, faults, ids = ledger
    with pytest.raises(RuntimeError):
        with store.connection() as db:
            db.execute(
                "INSERT INTO analysis_events(analysis_id,kind,agent,created) "
                "VALUES(?,'failure',?,?)",
                (ids[2], json.dumps({"code": "unknown"}), time.time()),
            )
            raise RuntimeError("abort original transaction")
    faults.tick()
    assert faults.list()["total"] == 0


def test_replaying_old_observation_never_reverses_a_later_state(ledger):
    store, faults, ids = ledger
    attempt(store, ids[2])
    source_event(store, ids[2], "required_read_validation", event(ids).diagnostic)
    source_event(store, ids[2], "required_read_retry", {"role": "repository_analyst"})
    source_event(
        store,
        ids[2],
        "failure",
        {
            "code": "analysis_scope_read_not_followed",
            "stage": "repository_analyst",
        },
    )
    faults.tick()
    group = faults.list()["items"][0]
    assert group["state"] == "blocked"
    before = faults.history(group["id"])["total"]
    with store.connection() as db:
        db.execute("UPDATE system_fault_outbox SET consumed=0")
    faults.tick()
    assert faults.list()["items"][0]["state"] == "blocked"
    assert faults.history(group["id"])["total"] == before


def test_delayed_acceptance_capture_uses_failure_budget_not_current_run(ledger):
    store, faults, ids = ledger
    seed_acceptance(store, ids)
    health = faults.configure(1, False)
    with store.connection() as db:
        db.execute(
            "INSERT INTO scope_acceptance_failures VALUES('original','acceptance_one',"
            "'acceptance_output_invalid',?,0,?)",
            (
                json.dumps(
                    {
                        "fault_basis": {
                            "attempt": 1,
                            "workbench_version": "old-build",
                            "budget": {"model_calls": 3, "max_model_calls": 12},
                        }
                    }
                ),
                time.time(),
            ),
        )
        db.execute("UPDATE scope_acceptance_runs SET attempt=2,model_calls=9")
    faults.configure(health["version"], True)
    faults.tick()
    group = faults.list(flow="acceptance")["items"][0]
    item = faults.detail(group["id"])["items"][0]
    assert item["attempt"] == 1 and item["budget"]["model_calls"] == 3
    assert item["workbench_version"] == "old-build"


def test_concurrent_consumers_and_paginated_detail_do_not_duplicate_records(ledger):
    from concurrent.futures import ThreadPoolExecutor

    store, faults, ids = ledger
    with store.connection() as db:
        for index in range(205):
            enqueue(db, event(ids, f"many{index}"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: SystemFaults(store).tick(), range(2)))
    group = faults.list()["items"][0]
    assert group["occurrences"] == 205
    first = faults.detail(group["id"], limit=200)
    last = faults.detail(group["id"], limit=200, offset=200)
    assert len(first["items"]) == 200 and len(last["items"]) == 5
    assert len({item["id"] for item in first["items"] + last["items"]}) == 205
    assert len(faults.history(group["id"], limit=200)["items"]) == 200
    assert len(faults.history(group["id"], offset=200)["items"]) == 5


def test_storage_failure_health_is_bounded_and_does_not_recurse(ledger, monkeypatch):
    from contextlib import contextmanager

    store, faults, _ = ledger

    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError("simulated storage outage")
        yield  # pragma: no cover

    monkeypatch.setattr(store, "connection", unavailable)
    assert faults.health()["error"] == "storage_unavailable"
    assert faults.tick() == 0
    assert store.fault_ingestion_error == "OperationalError"


def test_same_basis_across_tasks_groups_without_combining_their_states(ledger):
    store, faults, ids = ledger
    publish(store, event(ids, "task1"))
    publish(store, event(ids, "task2", task_id="analysis_other"))
    faults.tick()
    group = faults.list()["items"][0]
    assert faults.list()["total"] == 1 and group["occurrences"] == 2
    items = faults.detail(group["id"])["items"]
    assert {item["task_id"] for item in items} == {ids[2], "analysis_other"}
    faults.dismiss(group["id"], items[0]["id"], 1, "只关闭该任务")
    assert faults.list()["items"][0]["remaining"] == 1


def test_unknown_source_code_remains_pending_diagnosis(ledger):
    store, faults, ids = ledger
    source_event(store, ids[2], "failure", {"code": "new_unclassified_failure"})
    faults.tick()
    item = faults.list()["items"][0]
    assert item["state"] == "pending" and item["category"] == "unknown"


def test_real_repair_fields_and_constraints_survive_allowlisting(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(
        store,
        "tool_arguments_invalid",
        {
            "tool": "submit_and_test_repair",
            "reason": "arguments_schema",
            "fields": ["candidate.edits.[].content", "sk-private-value"],
            "issues": ["required_field", "string_required"],
            "corrections_remaining": 0,
            "input": "sk-private-value",
        },
    )
    faults.tick()
    item = faults.detail(faults.list()["items"][0]["id"])["items"][0]
    assert item["diagnostic"]["fields"] == ["candidate.edits.[].content"]
    assert item["diagnostic"]["issues"] == ["required_field", "string_required"]
    assert "sk-private-value" not in json.dumps(item)


def test_repair_timeout_wrapper_does_not_add_an_extra_occurrence(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(store, "fault_attempt", {"attempt": 1, "workbench_version": "build1"})
    for index in range(2):
        repair_source(
            store,
            "request_timeout",
            {
                "role": "repair_agent",
                "request": index + 1,
                "timeout_seconds": 120,
                "elapsed_seconds": 120.5,
            },
        )
    repair_source(store, "failure_details", {"code": "repair_model_timeout"})
    repair_source(store, "failure", "repair_model_timeout")
    faults.tick()
    assert sum(item["occurrences"] for item in faults.list()["items"]) == 2
    assert all(item["state"] == "blocked" for item in faults.list()["items"])


def test_delayed_execution_capture_keeps_original_revision(ledger):
    store, faults, ids = ledger
    health = faults.configure(1, False)
    now = time.time()
    with store.connection() as db:
        db.execute(
            "INSERT INTO execution_jobs(id,env_id,action,state,error,created,updated,fault_basis) "
            "VALUES('old_job',?,'test','failed','test_failed',?,?,?)",
            (ids[1], now, now, json.dumps({"revision": "a" * 40, "workbench_version": "old"})),
        )
        db.execute("UPDATE environments SET revision=?", ("b" * 40,))
    faults.configure(health["version"], True)
    faults.tick()
    item = faults.detail(faults.list()["items"][0]["id"])["items"][0]
    assert item["revision"] == "a" * 40


def acceptance_source(store, kind, value):
    with store.connection() as db:
        db.execute(
            "INSERT INTO task_scope_events(scope_id,version,kind,actor,value,created) "
            "VALUES('scope_one',1,?,'system',?,?)",
            (kind, json.dumps(value), time.time()),
        )


def test_acceptance_binding_never_borrows_previous_attempt(ledger):
    store, faults, ids = ledger
    seed_acceptance(store, ids)
    acceptance_source(
        store,
        "acceptance_model_configuration",
        {
            "run_id": "acceptance_one",
            "attempt": 1,
            "role": "independent_reviewer",
            "model": "old-model",
        },
    )
    with store.connection() as db:
        db.execute("UPDATE scope_acceptance_runs SET attempt=2")
        db.execute(
            "INSERT INTO scope_acceptance_failures VALUES('failure2','acceptance_one',"
            "'acceptance_output_invalid',?,0,?)",
            (
                json.dumps({"fault_basis": {"attempt": 2, "workbench_version": "build1"}}),
                time.time(),
            ),
        )
    faults.tick()
    item = faults.detail(faults.list()["items"][0]["id"])["items"][0]
    assert item["attempt"] == 2 and item["model_binding"] == {}


@pytest.mark.parametrize("changed_model", [False, True])
def test_repair_verified_retry_only_resolves_compatible_prior_attempt(ledger, changed_model):
    store, faults, ids = ledger
    seed_repair(store, ids)
    for number in (1, 2):
        repair_source(store, "fault_attempt", {"attempt": number, "workbench_version": "build1"})
        repair_source(store, "agent", "independent_reviewer")
        repair_source(
            store,
            "model_configuration",
            {
                "role": "independent_reviewer",
                "model": "new" if changed_model and number == 2 else "same",
                "model_version": 1,
            },
        )
        repair_source(store, "review_feedback" if number == 1 else "review_approved", {})
    faults.tick()
    assert faults.list()["items"][0]["state"] == ("pending" if changed_model else "resolved")


@pytest.mark.parametrize("changed_model", [False, True])
def test_acceptance_verified_retry_keeps_incompatible_model_unresolved(ledger, changed_model):
    store, faults, ids = ledger
    seed_acceptance(store, ids)
    acceptance_source(
        store,
        "acceptance_model_configuration",
        {
            "run_id": "acceptance_one",
            "attempt": 1,
            "role": "independent_reviewer",
            "model": "same",
            "model_version": 1,
        },
    )
    with store.connection() as db:
        db.execute(
            "INSERT INTO scope_acceptance_failures VALUES('failed1','acceptance_one',"
            "'acceptance_output_invalid',?,0,?)",
            (
                json.dumps({"fault_basis": {"attempt": 1, "workbench_version": "build1"}}),
                time.time(),
            ),
        )
        db.execute("UPDATE scope_acceptance_runs SET attempt=2")
    acceptance_source(
        store,
        "acceptance_model_configuration",
        {
            "run_id": "acceptance_one",
            "attempt": 2,
            "role": "independent_reviewer",
            "model": "new" if changed_model else "same",
            "model_version": 1,
        },
    )
    acceptance_source(
        store,
        "acceptance_completed",
        {
            "run_id": "acceptance_one",
            "attempt": 2,
            "workbench_version": "build1",
        },
    )
    faults.tick()
    assert faults.list()["items"][0]["state"] == ("pending" if changed_model else "resolved")


def test_legacy_repair_success_cannot_resolve_unknown_attempt_or_version(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(store, "agent", "independent_reviewer")
    repair_source(
        store,
        "model_configuration",
        {
            "role": "independent_reviewer",
            "model": "legacy",
            "model_version": 1,
        },
    )
    repair_source(store, "review_feedback", {})
    repair_source(store, "review_approved", {})
    faults.tick()
    group = faults.list()["items"][0]
    assert group["state"] == "pending" and group["remaining"] == 1


def test_unknown_repair_failure_wrapper_preserves_pending_diagnosis(ledger):
    store, faults, ids = ledger
    seed_repair(store, ids)
    repair_source(store, "failure_details", {"code": "new_unclassified_failure"})
    repair_source(store, "failure", "new_unclassified_failure")
    faults.tick()
    group = faults.list()["items"][0]
    assert group["state"] == "pending" and group["category"] == "unknown"
