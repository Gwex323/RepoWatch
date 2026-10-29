"""Candidate-to-publication regressions with local runtime/model/remote doubles only."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_execution import service  # noqa: F401
from test_repairs import Publisher, RepairEngine, RepairRuntime, enable, pending  # noqa: F401
from test_repository_analysis import Engine, prepare_tested

from repowatch.finding_eligibility import digest, initialize, origin, register
from repowatch.repairs import Repairs
from repowatch.repository_analysis import Analyses
from repowatch.store import Conflict, Store


def test_analysis_automatically_queues_and_publishes_without_owner_feedback(service):  # noqa: F811
    execution, repo, _ = service
    runtime = execution.runtime = RepairRuntime()
    env = prepare_tested((execution, repo, runtime))
    analyses = Analyses(execution, Engine())
    publisher = Publisher()
    repairs = Repairs(execution, RepairEngine(), analyses=analyses, publisher=publisher)
    analyses.repairer = repairs
    enable(repairs)
    assert asyncio.run(analyses.tick())
    report = analyses.list(env["id"])[0]
    assert report["status"] == "completed"
    finding = report["findings"][0]
    assert finding["status"] == "repair_candidate" and finding["feedback"] is None
    task = repairs.list(env["id"])[0]
    assert task["status"] == "queued" and task["context"]["confirmation"] is None
    assert task["context"]["repair_origin"]["kind"] == "analysis_review"
    assert asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "pr_created"
    assert len(publisher.calls) == 1
    assert "用户确认复现" not in publisher.calls[0][2]
    assert "实际自动验证" in publisher.calls[0][2]
    for section in ("概述", "变更内容", "设计方案/说明", "验证/验收", "review记录"):
        assert "## " + section in publisher.calls[0][2]
    repairs.backfill()
    assert len(repairs.list(env["id"])) == 1


@pytest.mark.parametrize("personally_reproduced", [False, True])
def test_manual_confirmation_is_retired_without_writes(pending, personally_reproduced):  # noqa: F811
    repairs, analyses, report, _, _ = pending
    finding = report["findings"][0]
    with pytest.raises(Conflict, match="manual_confirmation_retired"):
        analyses.feedback(report["id"], finding["id"], finding["version"],
                          "confirmed", "", personally_reproduced)
    assert analyses.get(report["id"])["findings"][0] == finding
    assert repairs.list(report["env_id"]) == []


def test_replay_and_concurrent_backfill_create_one_task(pending):  # noqa: F811
    repairs, _, report, _, _ = pending
    enable(repairs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: repairs.backfill(), range(2)))
    assert len(repairs.list(report["env_id"])) == 1


@pytest.mark.parametrize("status", ["waiting_reproduction", "not_reproduced", "false_positive",
                                   "resolved", "superseded"])
def test_upgrade_does_not_activate_pending_or_closed_history(pending, status):  # noqa: F811
    repairs, _, report, _, _ = pending
    with repairs.store.connection() as db:
        db.execute("DELETE FROM finding_repair_origins")
        db.execute("UPDATE analysis_findings SET status=?", (status,))
    enable(repairs)
    repairs.backfill()
    assert repairs.list(report["env_id"]) == []


def test_real_legacy_confirmation_remains_usable(pending):  # noqa: F811
    repairs, _, report, _, _ = pending
    finding = report["findings"][0]
    feedback = {"decision": "confirmed", "actor": "owner", "revision": report["revision"],
                "note": "真实历史记录的离线替身"}
    with repairs.store.connection() as db:
        db.execute("DELETE FROM finding_repair_origins")
        db.execute("UPDATE analysis_findings SET status='confirmed',feedback=? WHERE id=?",
                   (json.dumps(feedback), finding["id"]))
    enable(repairs)
    repairs.backfill()
    task = repairs.list(report["env_id"])[0]
    assert task["context"]["repair_origin"]["kind"] == "legacy_owner"
    assert task["context"]["confirmation"] == feedback


@pytest.mark.parametrize("mutation", ["missing_origin", "report", "finding", "paused"])
def test_candidate_requires_actual_current_provenance(pending, mutation):  # noqa: F811
    repairs, _, report, _, _ = pending
    with repairs.store.connection() as db:
        if mutation == "missing_origin":
            db.execute("DELETE FROM finding_repair_origins")
        elif mutation == "report":
            db.execute("UPDATE finding_repair_origins SET evidence='{}'")
        elif mutation == "finding":
            db.execute("UPDATE analysis_findings SET data='{}'")
        else:
            db.execute("UPDATE repositories SET paused=1")
    enable(repairs)
    repairs.backfill()
    assert repairs.list(report["env_id"]) == []


@pytest.mark.parametrize("running", [False, True])
def test_disposition_blocks_queued_and_running_candidate(pending, running):  # noqa: F811
    repairs, analyses, report, _, _ = pending
    enable(repairs)
    repairs.backfill()
    task = repairs.list(report["env_id"])[0]
    if running:
        with repairs.store.connection() as db:
            db.execute("UPDATE repair_tasks SET status='running' WHERE id=?", (task["id"],))
    finding = report["findings"][0]
    analyses.disposition(report["id"], finding["id"], finding["version"], "false_positive",
                         "分析误报，停止后续动作")
    with repairs.store.connection() as db:
        live = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        assert not repairs.current(db, live)
    repairs.backfill()
    assert len(repairs.list(report["env_id"])) == 1
    assert not repairs.get(task["id"])["can_retry"]


def test_repeat_initialization_and_transaction_rollback_preserve_facts(pending):  # noqa: F811
    repairs, _, report, _, _ = pending
    enable(repairs)
    repairs.backfill()
    task = repairs.list(report["env_id"])[0]
    Store(repairs.store.path)
    with repairs.store.connection() as db:
        initialize(db)
        analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (report["id"],)).fetchone()
        finding = db.execute("SELECT * FROM analysis_findings WHERE id=?",
                             (task["finding_id"],)).fetchone()
        assert origin(db, analysis, finding)["kind"] == "analysis_review"
    with pytest.raises(RuntimeError), repairs.store.connection() as db:
        db.execute("DELETE FROM finding_repair_origins")
        raise RuntimeError("rollback fixture")
    assert repairs.settings()["enabled"]
    assert repairs.get(task["id"])["attempt"] == 0
    with repairs.store.connection() as db:
        assert db.execute("SELECT count(*) FROM finding_repair_origins").fetchone()[0] == 1


def test_bounded_backfill_does_not_starve_valid_candidates(pending):  # noqa: F811
    repairs, _, report, _, _ = pending
    with repairs.store.connection() as db:
        for index in range(101):
            db.execute("INSERT INTO analysis_findings(id,analysis_id,status,data) "
                       "SELECT ?,analysis_id,'repair_candidate',data FROM analysis_findings "
                       "WHERE id=?", (f"aaa_invalid_{index}", report["findings"][0]["id"]))
    enable(repairs)
    repairs.backfill()
    assert repairs.list(report["env_id"]) == []
    repairs.backfill()
    assert len(repairs.list(report["env_id"])) == 1


def test_scope_rereview_preserves_immutable_analysis_origin(pending):  # noqa: F811
    repairs, _, report, _, _ = pending
    with repairs.store.connection() as db:
        analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (report["id"],)).fetchone()
        finding = db.execute("SELECT * FROM analysis_findings WHERE id=?",
                             (report["findings"][0]["id"],)).fetchone()
        revised = {**json.loads(analysis["report"]), "summary": "独立范围复审后的报告"}
        db.execute("UPDATE repository_analyses SET report=? WHERE id=?",
                   (json.dumps(revised), analysis["id"]))
        changed = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                             (analysis["id"],)).fetchone()
        before = origin(db, changed, finding)
        assert before["kind"] == "analysis_review"
        register(db, changed, finding["id"], "analysis_review", {"report_digest": digest(revised)})
        assert origin(db, changed, finding) == before


def scoped_candidate(fixture):
    """Bind a local fixture candidate to one original requirement before actual repair execution."""
    from repowatch.task_scopes import register_scope, scope_packet

    repairs, analyses, report, _, _ = fixture
    with repairs.store.connection() as db:
        analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (report["id"],)).fetchone()
        scope = register_scope(db, analysis, [{
            "key": "boundary", "title": "原边界要求", "expected": "边界值返回正确结果",
            "paths": ["app.py"], "required_evidence": ["regression"],
        }], "owner")
        finding = report["findings"][0]
        data = {**finding["data"], "scope_keys": ["boundary"]}
        db.execute("UPDATE analysis_findings SET data=? WHERE id=?",
                   (json.dumps(data), finding["id"]))
        db.execute("DELETE FROM finding_repair_origins WHERE finding_id=?", (finding["id"],))
        register(db, analysis, finding["id"], "analysis_review",
                 {"report_digest": digest(json.loads(analysis["report"]))})
        db.execute("INSERT INTO task_scope_findings VALUES(?,?,?)",
                   (scope["id"], "boundary", finding["id"]))
        assert scope_packet(db, analysis["id"])["items"]
    enable(repairs)
    repairs.backfill()
    assert asyncio.run(repairs.tick())
    task = repairs.list(report["env_id"])[0]
    assert task["status"] == "pr_created"
    return task


@pytest.mark.parametrize("kind", ["regression", "integration"])
def test_published_candidate_acceptance_creates_bounded_evidence_followup(pending, kind):  # noqa: F811
    from repowatch.acceptance import MAX_FOLLOWUPS, Acceptance
    from repowatch.acceptance_model import AcceptanceVerdict
    from repowatch.task_scopes import scope_packet

    repairs, analyses, report, _, publisher = pending
    parent = scoped_candidate(pending)
    worker = Acceptance(analyses, repairs, repairs.engine)
    now = time.time()
    verdict = AcceptanceVerdict(met=False, behavioral_failure=True,
                                rationale="原范围断言失败（离线替身）", corrected_files=[])
    result = {"setup_exit_code": 0, "exit_code": 1, "log": "1 failed: assertion mismatch"}
    with repairs.store.connection() as db:
        analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (report["id"],)).fetchone()
        assert worker._authorized(db, analysis)
        assert [t["id"] for t in worker._repairs(db, analysis)] == [parent["id"]]
        scope = scope_packet(db, analysis["id"])
        basis = worker._basis(db, analysis, worker._repairs(db, analysis))
        db.execute("INSERT INTO scope_acceptance_runs "
                   "(id,scope_id,version,analysis_id,basis,basis_hash,state,owner,created,updated) "
                   "VALUES('run97',?,?,?,?,?,'running',?,?,?)",
                   (scope["id"], scope["version"], analysis["id"], json.dumps(basis),
                    digest(basis), worker.owner, now, now))
        db.execute("INSERT INTO scope_acceptance_checks "
                   "(id,run_id,item_key,kind,repair_id,state,specification,result,created,updated) "
                   "VALUES('check97','run97','boundary',?,?,'running',?,?,?,?)",
                   (kind, parent["id"], json.dumps({"files": []}), json.dumps(result), now, now))
        run = dict(db.execute("SELECT * FROM scope_acceptance_runs WHERE id='run97'").fetchone())
        check = dict(db.execute(
            "SELECT * FROM scope_acceptance_checks WHERE id='check97'",
        ).fetchone())
    worker._save_result(run, check, result, verdict, False)
    worker._followup(run, check, verdict, result)
    with repairs.store.connection() as db:
        receipt = db.execute("SELECT * FROM scope_acceptance_followups").fetchone()
        assert receipt["root_finding_id"] == parent["finding_id"]
        child = db.execute("SELECT * FROM analysis_findings WHERE id=?",
                           (receipt["finding_id"],)).fetchone()
        assert child["status"] == "repair_candidate"
        assert json.loads(child["feedback"])["actor"] == "system"
        analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                              (report["id"],)).fetchone()
        assert origin(db, analysis, child)["kind"] == "acceptance_followup"
        assert db.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0] == 2
        # A replay cannot mint another child, and the existing cap remains unchanged.
        assert MAX_FOLLOWUPS == 2
        child_id = child["id"]
    with pytest.raises(Conflict, match="acceptance_basis_changed"):
        worker._followup(run, check, verdict, result)
    assert len(publisher.calls) == 1
    with repairs.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM scope_acceptance_followups").fetchone()[0] == 1
        db.execute("UPDATE scope_acceptance_checks SET result='{}' WHERE id='check97'")
        child = db.execute("SELECT * FROM analysis_findings WHERE id=?", (child_id,)).fetchone()
        assert origin(db, analysis, child) is None


@pytest.fixture
def rechecked_history(pending):  # noqa: F811
    from repowatch.historical_review import historical_snapshot as snapshot
    from repowatch.task_scopes import ensure_scope, register_scope, save_scope_review, scope_packet

    repairs, analyses, report, _, _ = pending
    old_id = report["findings"][0]["id"]
    with repairs.store.connection() as db:
        original = dict(db.execute("SELECT * FROM repository_analyses WHERE id=?",
                                   (report["id"],)).fetchone())
        db.execute("DELETE FROM finding_repair_origins WHERE finding_id=?", (old_id,))
        db.execute("UPDATE analysis_findings SET status='waiting_reproduction' WHERE id=?",
                   (old_id,))
        db.execute("UPDATE repository_analyses SET trigger_key='old-trigger97' WHERE id=?",
                   (original["id"],))
        target = {**original, "id": "analysis_recheck97", "created": original["created"] + 1}
        context = json.loads(original["context"])
        context["historical_findings"] = snapshot(
            db, original["repo_id"], target["id"], target["created"],
        )
        target["context"] = json.dumps(context)
        db.execute("INSERT INTO repository_analyses (" + ",".join(target) + ") VALUES ("
                   + ",".join("?" for _ in target) + ")", tuple(target.values()))
        ensure_scope(db, target)
        scope = register_scope(db, target, [{
            "key": "boundary", "title": "复查原问题", "expected": "原边界要求",
            "paths": ["app.py"], "required_evidence": ["regression"],
        }], "owner")
        context["task_scope"] = scope_packet(db, target["id"])
        reviewed = {**json.loads(original["report"]), "findings": [],
                    "historical_rechecks": [{
                        "finding_id": old_id, "outcome": "still_present",
                        "rationale": "当前源码仍存在",
                        "evidence": report["findings"][0]["data"]["evidence"],
                    }], "scope_review": {
                        "version": scope["version"], "accepted": False, "rationale": "仍需修复",
                        "items": [{"key": "boundary", "outcome": "confirmed",
                                   "rationale": "当前范围证据支持",
                                   "evidence": report["findings"][0]["data"]["evidence"],
                                   "finding_ids": []}], "exclusions": [],
                    }}
        db.execute("UPDATE repository_analyses SET context=?,report=? WHERE id=?",
                   (json.dumps(context), json.dumps(reviewed), target["id"]))
        save_scope_review(db, target, reviewed["scope_review"], [])
    return repairs, analyses, report, target["id"]


def test_current_recheck_continues_old_pending_without_forging_confirmation(rechecked_history):
    repairs, analyses, old, target = rechecked_history
    enable(repairs)
    repairs.backfill()
    assert repairs.list(old["env_id"]) == []
    finding = old["findings"][0]
    values = {"target_analysis_id": target, "analysis_version": old["version"],
              "note": "依据当前版本复查接续"}
    result = analyses.continue_finding(old["id"], finding["id"], version=1, **values)
    task = repairs.get(result["repair_id"])
    assert task["context"]["repair_origin"]["kind"] == "current_recheck"
    assert task["context"]["confirmation"] is None
    assert analyses.get(old["id"])["findings"][0]["status"] == "superseded"
    with pytest.raises(Conflict, match="finding_changed"):
        analyses.continue_finding(old["id"], finding["id"], version=1, **values)
    assert len(repairs.list(old["env_id"])) == 1
    with repairs.store.connection() as db:
        live = db.execute("SELECT * FROM repair_tasks WHERE id=?", (task["id"],)).fetchone()
        assert repairs.current(db, live)
        db.execute("UPDATE repository_analyses SET report='{}' WHERE id=?", (target,))
        assert not repairs.current(db, live)


@pytest.mark.parametrize("mutation", ["source", "recheck", "scope", "closed"])
def test_current_recheck_requires_current_evidence_and_open_history(rechecked_history, mutation):
    repairs, analyses, old, target = rechecked_history
    finding = old["findings"][0]
    if mutation == "source":
        path = repairs.execution.directory(old["env_id"]) / "source" / "app.py"
        path.write_text("print('changed')", encoding="utf-8")
    else:
        with repairs.store.connection() as db:
            if mutation == "closed":
                db.execute("UPDATE analysis_findings SET status='false_positive' WHERE id=?",
                           (finding["id"],))
            else:
                raw = json.loads(db.execute("SELECT report FROM repository_analyses WHERE id=?",
                                            (target,)).fetchone()[0])
                if mutation == "recheck":
                    raw["historical_rechecks"][0]["outcome"] = "not_covered"
                else:
                    raw["scope_review"]["items"][0]["outcome"] = "investigating"
                db.execute("UPDATE repository_analyses SET report=? WHERE id=?",
                           (json.dumps(raw), target))
    with pytest.raises(Conflict):
        analyses.continue_finding(old["id"], finding["id"], version=1,
                                  target_analysis_id=target, analysis_version=old["version"],
                                  note="不能越过依据检查")
    assert repairs.list(old["env_id"]) == []


def test_disposition_during_remote_request_keeps_actual_receipt(pending):  # noqa: F811
    repairs, analyses, report, _, publisher = pending
    finding = report["findings"][0]
    original_publish = publisher.publish

    def in_flight(task, patch, body, *, check):
        check()
        analyses.disposition(report["id"], finding["id"], finding["version"],
                             "false_positive", "远程请求已发出时处置")
        # This request has already been sent; its result is a fact, not a new action.
        return original_publish(task, patch, body, check=lambda: None)

    publisher.publish = in_flight
    enable(repairs)
    repairs.backfill()
    assert asyncio.run(repairs.tick())
    task = repairs.list(report["env_id"])[0]
    assert task["status"] == "pr_created" and task["remote"]["pr"]["number"] == 2
    assert not task["can_retry"]
    repairs.backfill()
    assert len(publisher.calls) == 1



def test_metrics_separate_candidates_from_real_legacy_confirmations(pending):  # noqa: F811
    from repowatch.dashboard import snapshot
    from repowatch.memory import Memory

    repairs, _, report, _, _ = pending
    with repairs.store.connection() as db:
        for identity, status, feedback in [
            ("history_pending97", "waiting_reproduction", None),
            ("history_legacy97", "confirmed", json.dumps({
                "actor": "owner", "decision": "confirmed", "revision": report["revision"],
            })),
            ("history_invalid97", "confirmed", None),
        ]:
            db.execute("INSERT INTO analysis_findings(id,analysis_id,status,data,feedback) "
                       "SELECT ?,analysis_id,?,data,? FROM analysis_findings WHERE id=?",
                       (identity, status, feedback, report["findings"][0]["id"]))
        view = snapshot(db)
        assert view["counts"]["waiting"] == view["counts"]["repair_candidates"] == 1
        assert len(view["findings"]) == 4
    counts = Memory(repairs.store).metrics(report["repo_id"])["without_memory"]
    assert counts["repair_candidates"] == counts["confirmed_findings"] == 1
    assert counts["findings"] == 4
    enable(repairs)
    repairs.backfill()
    with repairs.store.connection() as db:
        assert snapshot(db)["counts"]["repair_candidates"] == 0
        assert db.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0] == 2
        # The old binary's positive gate cannot see a candidate as a confirmation.
        assert db.execute("SELECT COUNT(*) FROM analysis_findings "
                          "WHERE status='confirmed' AND id=?",
                          (report["findings"][0]["id"],)).fetchone()[0] == 0



@pytest.mark.parametrize("enabled", [False, True])
def test_old_schema_upgrade_preserves_settings_budgets_feedback_and_receipts(pending, enabled):  # noqa: F811
    repairs, _, report, _, _ = pending
    enable(repairs)
    repairs.backfill()
    assert asyncio.run(repairs.tick())
    if not enabled:
        repairs.configure(repairs.settings()["version"], False)
    tables = ("analysis_findings", "repair_tasks", "repair_settings", "repair_events",
              "recovery_roots", "recovery_actions", "recovery_usage")
    with repairs.store.connection() as db:
        feedback = {"actor": "owner", "decision": "confirmed", "revision": report["revision"]}
        db.execute("UPDATE analysis_findings SET status='confirmed',feedback=?",
                   (json.dumps(feedback),))
        db.execute("DROP TABLE finding_repair_origins")
        before = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                  for table in tables}
    Store(repairs.store.path)
    Store(repairs.store.path)
    with repairs.store.connection() as db:
        after = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                 for table in tables}
        assert after == before
        assert db.execute("SELECT COUNT(*) FROM finding_repair_origins").fetchone()[0] == 0
    assert repairs.settings()["enabled"] is enabled
    repairs.backfill()
    assert len(repairs.list(report["env_id"])) == 1


def test_final_report_and_candidate_origin_roll_back_together(service, monkeypatch):  # noqa: F811
    execution, repo, _ = service
    env = prepare_tested((execution, repo, execution.runtime))
    analyses = Analyses(execution, Engine())
    from repowatch import repository_analysis

    original_register = repository_analysis.register_repair_origin

    def fail_after_origin(*args, **kwargs):
        original_register(*args, **kwargs)
        raise RuntimeError("offline final transaction failure")

    monkeypatch.setattr(repository_analysis, "register_repair_origin", fail_after_origin)
    assert asyncio.run(analyses.tick())
    report = analyses.list(env["id"])[0]
    assert report["status"] == "failed" and report["report"] is None
    assert report["findings"] == []
    with execution.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM finding_repair_origins").fetchone()[0] == 0
