"""Acceptance for bounded automatic final-review recovery."""

import asyncio
import json
from types import SimpleNamespace

from test_analysis_report_recovery import RecoveryEngine
from test_execution import service  # noqa: F401
from test_repository_analysis import prepare_tested

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


def test_automatic_retries_keep_the_same_persistent_budget_and_stop_at_limit(service):  # noqa: F811
    analyses, engine, queued = failed_review(service)
    analysis_id = queued["id"]
    initial_requests = queued["automatic_recovery"]["requests"]

    for expected_resume in (2, 2):
        with analyses.store.connection() as db:
            db.execute("UPDATE analysis_report_recovery SET retry_after=0 "
                       "WHERE analysis_id=?", (analysis_id,))
        asyncio.run(analyses.tick())
        current = analyses.get(analysis_id)
        assert current["automatic_recovery"]["resumptions"] == expected_resume

    stopped = analyses.get(analysis_id)
    assert stopped["status"] == "failed"
    assert stopped["automatic_recovery"]["state"] == "stopped"
    assert stopped["automatic_recovery"]["stop_reason"] == "resumption_limit"
    assert stopped["automatic_recovery"]["requests"] == initial_requests + 2
    assert stopped["report"] is None
    assert sum(event["kind"] == "report_recovery_scheduled"
               for event in stopped["events"]) == 2
    assert stopped["events"][-1]["kind"] == "report_recovery_stopped"
