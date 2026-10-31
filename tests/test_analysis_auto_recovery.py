"""Persistent usage and automatic final-review recovery without quotas."""

import asyncio
import json
import time
from types import SimpleNamespace

from test_analysis_report_recovery import RecoveryEngine
from test_execution import service  # noqa: F401
from test_repository_analysis import prepare_tested

from repowatch import report_scheduler
from repowatch.repository_analysis import Analyses, report_diagnostics


def failed_review(execution_fixture):
    execution, _, _ = execution_fixture
    env = prepare_tested(execution_fixture)
    engine = RecoveryEngine(fail=True)
    if hasattr(engine, "fail_code"):
        engine.fail_code = "analysis_provider_timeout_exhausted"
    analyses = Analyses(execution, engine)
    analysis_id = analyses.enqueue(env["id"])
    asyncio.run(analyses.tick())
    result = analyses.get(analysis_id)
    assert result["status"] == "queued"
    assert result["report"] is None and not result["report_recovery"]["available"]
    assert result["automatic_recovery"]["state"] == "waiting"
    assert result["automatic_recovery"]["resumptions"] == 1
    return analyses, engine, result


def make_response(text):
    return SimpleNamespace(output=[SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text=text)],
    )])


def test_json_diagnostics_are_actionable_and_never_store_candidate_text():
    secret_candidate = '{"summary":"candidate-private-text"'
    details = json.loads(report_diagnostics(make_response(secret_candidate)))
    assert details["category"] == "invalid_json"
    assert details["line"] == 1 and details["column"] > 1
    assert details["reason"] == "expected_delimiter"
    assert "candidate-private-text" not in json.dumps(details)


def test_repeated_failure_fingerprint_requires_two_identical_consecutive_failures(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)
    analysis_id = Analyses(execution, RecoveryEngine()).enqueue(env["id"])
    with execution.store.connection() as db:
        for fingerprint in ("same-safe-hash", "same-safe-hash"):
            db.execute(
                "INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
                (analysis_id, "failure", json.dumps({
                    "code": "scope_review_incomplete",
                    "progress_fingerprint": fingerprint,
                }), time.time()),
            )
        assert report_scheduler.repeated_no_progress(db, analysis_id)
        db.execute(
            "INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
            (analysis_id, "failure", json.dumps({
                "code": "scope_review_incomplete",
                "progress_fingerprint": "changed-safe-hash",
            }), time.time()),
        )
        assert not report_scheduler.repeated_no_progress(db, analysis_id)


def test_in_attempt_no_progress_failure_stops_automatic_recovery(service):  # noqa: F811
    analyses, _, queued = failed_review(service)
    with analyses.store.connection() as db:
        db.execute(
            "UPDATE repository_analyses SET status='failed',error='analysis_report_no_progress',"
            "version=version+1 WHERE id=?", (queued["id"],),
        )
        failed = db.execute(
            "SELECT * FROM repository_analyses WHERE id=?", (queued["id"],),
        ).fetchone()
        assert not analyses.schedule_report_recovery(
            db, failed, "analysis_report_no_progress", time.time(),
        )
        recovery = db.execute(
            "SELECT state,stop_reason FROM analysis_report_recovery WHERE analysis_id=?",
            (queued["id"],),
        ).fetchone()
    assert dict(recovery) == {"state": "stopped", "stop_reason": "no_progress"}


def test_schema_diagnostics_use_only_known_field_labels():
    details = json.loads(report_diagnostics(make_response(
        '{"private-attacker-field":"do-not-repeat"}',
    )))
    assert details["category"] == "schema_mismatch"
    assert any(item["path"] == "[unknown]" for item in details["fields"])
    assert "private-attacker-field" not in json.dumps(details)
    assert "do-not-repeat" not in json.dumps(details)


def test_valid_checkpoint_is_automatically_resumed_without_manual_retry(service):  # noqa: F811
    analyses, engine, queued = failed_review(service)
    before = queued["automatic_recovery"]["requests"]
    engine.fail = False
    with analyses.store.connection() as db:
        db.execute("UPDATE analysis_report_recovery SET retry_after=0 WHERE analysis_id=?",
                   (queued["id"],))

    asyncio.run(analyses.tick())
    completed = analyses.get(queued["id"])
    assert completed["status"] == "completed", completed["error"]
    assert completed["automatic_recovery"]["state"] == "completed"
    assert completed["automatic_recovery"]["requests"] == before + 1
    assert len(engine.requests) == before + 1
    packet, kwargs = engine.requests[-1]
    assert not kwargs["tools"] and kwargs["model_settings"].tool_choice == "none"
    assert json.loads(packet)["execution_was_read"]
    assert any(item["kind"] == "report_resumed" for item in completed["events"])


def test_automatic_retries_keep_usage_beyond_the_old_limit(service):  # noqa: F811
    analyses, engine, queued = failed_review(service)
    analysis_id = queued["id"]
    initial_requests = queued["automatic_recovery"]["requests"]

    for expected_resume in (2, 3, 4):
        with analyses.store.connection() as db:
            db.execute("UPDATE analysis_report_recovery SET retry_after=0 "
                       "WHERE analysis_id=?", (analysis_id,))
        asyncio.run(analyses.tick())
        current = analyses.get(analysis_id)
        assert current["automatic_recovery"]["resumptions"] == expected_resume

    stopped = analyses.get(analysis_id)
    assert stopped["status"] == "queued"
    assert stopped["automatic_recovery"]["state"] == "waiting"
    assert stopped["automatic_recovery"]["stop_reason"] is None
    assert stopped["automatic_recovery"]["max_resumptions"] is None
    assert stopped["automatic_recovery"]["requests"] == initial_requests + 3
    assert stopped["report"] is None
    scheduled = [json.loads(event["agent"]) for event in stopped["events"]
                 if event["kind"] == "report_recovery_scheduled"]
    assert [item["delay_seconds"] for item in scheduled] == [10, 30, 60, 60]
    assert stopped["automatic_recovery"]["deadline"] is None


def test_third_manual_continuation_preserves_existing_budget_after_restart(service):  # noqa: F811
    analyses, engine, queued = failed_review(service)
    analysis_id = queued["id"]
    with analyses.store.connection() as db:
        before = dict(db.execute("SELECT * FROM analysis_report_recovery WHERE analysis_id=?",
                                 (analysis_id,)).fetchone())
        db.execute("UPDATE repository_analyses SET status='failed',version=version+1 WHERE id=?",
                   (analysis_id,))
        db.execute("UPDATE analysis_report_recovery SET state='stopped',resumptions=2,"
                   "retry_after=0,stop_reason='resumption_limit' WHERE analysis_id=?",
                   (analysis_id,))

    restarted = Analyses(analyses.execution, engine)
    failed = restarted.get(analysis_id)
    retried = restarted.retry(analysis_id, failed["version"])
    assert retried["status"] == "queued"
    assert retried["automatic_recovery"]["resumptions"] == 3
    assert retried["automatic_recovery"]["max_resumptions"] is None
    assert retried["automatic_recovery"]["requests"] == before["requests"]
    with restarted.store.connection() as db:
        after = dict(db.execute("SELECT * FROM analysis_report_recovery WHERE analysis_id=?",
                                (analysis_id,)).fetchone())
    assert after["deadline"] == before["deadline"]
    assert after["retry_budget"] == before["retry_budget"]

    engine.fail = False
    asyncio.run(restarted.tick())
    completed = restarted.get(analysis_id)
    assert completed["status"] == "completed", completed["error"]
    assert completed["automatic_recovery"]["requests"] == before["requests"] + 1
