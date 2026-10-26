"""Historical rechecks are evidence-bound observations, not repair authority."""

import asyncio
import copy
import json

import pytest
from test_execution import service  # noqa: F401
from test_repository_analysis import Engine, ReviewModel, prepare_tested

from repowatch.historical_review import (
    MAX_HISTORICAL_FINDINGS,
    historical_results,
    historical_snapshot,
)
from repowatch.repository_analysis import (
    ROLES,
    Analyses,
    AnalysisFailure,
    Review,
    evidence_issues,
    normalize_evidence_quotes,
    run_analysis,
    validate_report,
)


def context():
    return {
        "files": {"app.py": "print('ok')\nnot_read()"},
        "config": {}, "jobs": [], "test_result": None,
        "historical_findings": {"total": 1, "included": 1, "omitted": 0, "items": [{
            "finding_id": "finding_old", "version": 2, "human_status": "confirmed",
            "analysis_id": "analysis_old", "revision": "a" * 40,
            "data": {"title": "Legacy claim"}, "content_clipped": False,
        }]},
    }


def recheck(outcome="still_present"):
    return {
        "finding_id": "finding_old", "outcome": outcome,
        "rationale": "Compare the historical claim against the current source.",
        "evidence": [{"path": "app.py", "start_line": 1, "end_line": 1,
                      "quote": "print('ok')"}] if outcome != "not_covered" else [],
    }


def report(items):
    return {
        "summary": "Fixture, not a real diagnosis.", "limitations": ["Source review only."],
        "findings": [], "historical_rechecks": items,
        "files_read": ["app.py"], "read_ranges": {"app.py": [[1, 1]]},
        "execution_read": True,
        "handoffs": [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])],
    }


@pytest.mark.parametrize("outcome", [
    "still_present", "fixed", "suspected_false_positive", "not_covered",
])
def test_all_four_outcomes_require_separate_current_evidence(outcome):
    data = report([recheck(outcome)])
    parsed = validate_report(data, context())
    assert parsed.findings == []
    assert parsed.historical_rechecks[0].outcome == outcome
    if outcome != "not_covered":
        data["historical_rechecks"][0]["evidence"] = []
        with pytest.raises(ValueError, match="analysis_evidence_unverified"):
            validate_report(data, context())


def test_legacy_and_omitted_decisions_are_never_treated_as_fixed():
    legacy = report([])
    del legacy["historical_rechecks"]
    parsed = validate_report(legacy, context())
    assert parsed.historical_rechecks[0].outcome == "not_covered"
    assert not parsed.historical_rechecks[0].evidence
    assert "不能" in parsed.historical_rechecks[0].rationale
    no_history = {**context(), "historical_findings": {"items": []}}
    assert validate_report(legacy, no_history).historical_rechecks == []
    assert historical_results({}, legacy) is None
    assert historical_results(context(), None) is None


@pytest.mark.parametrize("change", ["unknown_id", "duplicate", "unread_file", "unread_line",
                                    "old_quote", "missing_file", "invalid_range"])
def test_forged_or_historical_only_evidence_is_rejected(change):
    item = recheck("fixed")
    evidence = item["evidence"][0]
    if change == "unknown_id":
        item["finding_id"] = "finding_other_repository"
    elif change == "unread_file":
        evidence["path"] = "unread.py"
    elif change == "unread_line":
        evidence.update(start_line=2, end_line=2, quote="not_read()")
    elif change == "old_quote":
        evidence["quote"] = "old_function()"
    elif change == "missing_file":
        evidence["path"] = "removed.py"
    elif change == "invalid_range":
        evidence["end_line"] = 999
    data = report([item, copy.deepcopy(item)] if change == "duplicate" else [item])
    ctx = context()
    ctx["files"]["unread.py"] = "print('ok')"
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        validate_report(data, ctx)
    issues = evidence_issues(Review.model_validate({key: data[key] for key in Review.model_fields}),
                             ctx, {"app.py"}, data["read_ranges"])
    assert issues and all(issue["historical"] for issue in issues)
    assert "print('ok')" not in json.dumps(issues)


def test_not_covered_still_cannot_include_forged_evidence():
    item = recheck("not_covered")
    item["evidence"] = recheck()["evidence"]
    item["evidence"][0]["quote"] = "invented()"
    with pytest.raises(ValueError, match="analysis_evidence_unverified"):
        validate_report(report([item]), context())


def test_display_line_normalization_also_covers_historical_quotes():
    item = recheck()
    item["evidence"][0]["quote"] = "1: print('ok')"
    parsed = Review.model_validate({key: report([item])[key] for key in Review.model_fields})
    assert normalize_evidence_quotes(parsed, context(), {"app.py"}, {"app.py": [[1, 1]]}) == 1
    assert parsed.historical_rechecks[0].evidence[0].quote == "print('ok')"


class HistoricalModel(ReviewModel):
    def __init__(self, items):
        super().__init__(findings=False)
        self.items = items
        self.inputs = []

    async def get_response(self, system_instructions, input, **kwargs):
        self.inputs.append((system_instructions, input))
        response = await super().get_response(system_instructions, input, **kwargs)
        for output in response.output:
            if output.type == "message":
                data = json.loads(output.content[0].text)
                if "corrections" in data:
                    withdraw = bool(self.items and self.items[0]["outcome"] == "not_covered")
                    data["summary"] = "Unsupported history is not covered." if withdraw else None
                    for item in data["corrections"]:
                        item.update(withdraw=withdraw, edits=[],
                                    rationale="Synthetic historical correction.")
                else:
                    data["historical_rechecks"] = self.items
                output.content[0].text = json.dumps(data)
        return response


@pytest.mark.parametrize("outcome", [
    "still_present", "fixed", "suspected_false_positive", "not_covered", None,
])
def test_native_sdk_runs_rechecks_without_new_findings_or_fabricated_handoffs(outcome):
    model = HistoricalModel([recheck(outcome)] if outcome else [])
    data = asyncio.run(run_analysis(model, context()))
    assert not data["findings"]
    assert len(data["handoffs"]) == 3
    assert data["historical_rechecks"][0]["outcome"] == (outcome or "not_covered")
    assert data["files_read"] == ["app.py"]
    instructions, intro = model.inputs[0]
    assert "correct rejection" in instructions
    assert "security bypass" in instructions
    origins = json.loads(intro[0]["content"])["historical_findings"]["items"]
    assert origins[0]["finding_id"] == "finding_old"


@pytest.fixture
def history(service):  # noqa: F811
    execution, repo, _ = service
    env = prepare_tested(service)
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    old = analyses.list(env["id"])[0]
    finding = old["findings"][0]
    analyses.feedback(old["id"], finding["id"], finding["version"], "confirmed", "", True)
    with execution.store.connection() as db:
        db.execute("UPDATE repository_analyses SET revision=? WHERE id=?", ("b" * 40, old["id"]))
    return execution, repo, env, analyses, old, finding


def test_snapshot_is_cross_revision_bounded_and_repo_scoped(history):
    execution, repo, env, analyses, old, finding = history
    other_repo = execution.store.add_repository("other/project", "https://github.com/other/project")
    with execution.store.connection() as db:
        # A completed unrelated analysis with an otherwise identical claim is not input.
        db.execute(
            "INSERT INTO repository_analyses SELECT 'analysis_other',env_id,?,revision,"
            "fingerprint,'other-trigger',status,context,report,error,version,lease,created,updated "
            "FROM repository_analyses WHERE id=?", (other_repo["id"], old["id"]),
        )
        db.execute("INSERT INTO analysis_findings(id,analysis_id,data,status) "
                   "SELECT 'finding_other','analysis_other',data,'confirmed' "
                   "FROM analysis_findings WHERE id=?", (finding["id"],))
        for index in range(MAX_HISTORICAL_FINDINGS + 3):
            db.execute("INSERT INTO analysis_findings(id,analysis_id,data,status) "
                       "SELECT ?,analysis_id,data,'waiting_reproduction' "
                       "FROM analysis_findings WHERE id=?",
                       (f"finding_extra_{index}", finding["id"]))
        db.execute("INSERT INTO analysis_findings(id,analysis_id,data,status) "
                   "SELECT 'finding_rejected',analysis_id,data,'not_reproduced' "
                   "FROM analysis_findings WHERE id=?", (finding["id"],))
        snapshot = historical_snapshot(db, repo["id"], "analysis_new", old["created"] + 1)
        assert snapshot["included"] == MAX_HISTORICAL_FINDINGS
        assert snapshot["total"] == MAX_HISTORICAL_FINDINGS + 4
        assert snapshot["omitted"] == 4
        first = snapshot["items"][0]
        assert first["finding_id"] == finding["id"]
        assert first["version"] == 2 and first["human_status"] == "confirmed"
        assert first["revision"] == "b" * 40
        assert all(item["finding_id"] not in ("finding_other", "finding_rejected")
                   for item in snapshot["items"])
        assert historical_snapshot(db, repo["id"], old["id"], old["created"])["total"] == 0
        assert historical_snapshot(db, repo["id"], "new", old["created"] - 1)["total"] == 0


def test_history_is_clipped_and_redacted_before_model_input(history):
    execution, repo, _, _, old, finding = history
    with execution.store.connection() as db:
        data = json.loads(db.execute("SELECT data FROM analysis_findings WHERE id=?",
                                     (finding["id"],)).fetchone()[0])
        data["reproduction"] = ["x" * 100000]
        data["explanation"] = "ghp_not_a_real_test_credential " + "y" * 10000
        db.execute("UPDATE analysis_findings SET data=? WHERE id=?",
                   (json.dumps(data), finding["id"]))
        snapshot = historical_snapshot(db, repo["id"], "new", old["created"] + 1)
    text = json.dumps(snapshot)
    assert len(text) < 6000 and "ghp_not_a_real_test_credential" not in text
    assert "[REDACTED]" in text
    assert snapshot["items"][0]["content_clipped"]


@pytest.mark.parametrize("outcome", ["still_present", "fixed", "suspected_false_positive", None])
def test_durable_results_never_overwrite_human_feedback_or_queue_repairs(history, outcome):
    execution, _, env, analyses, old, finding = history

    class RecheckEngine(Engine):
        async def analyze(self, ctx, event):
            assert ctx["historical_findings"]["items"][0]["revision"] == "b" * 40
            item = recheck(outcome) if outcome else None
            if item:
                item["finding_id"] = finding["id"]
            return await run_analysis(HistoricalModel([item] if item else []), ctx, event)

    analyses.engine = RecheckEngine()
    execution.action(env["id"], execution.get(env["id"])["version"], "test")
    execution.tick()
    asyncio.run(analyses.tick())
    current = analyses.list(env["id"])[0]
    assert current["status"] == "completed" and current["findings"] == []
    view = current["historical_review"]
    assert view["total"] == 1 and view["omitted"] == 0
    assert view["items"][0]["result"]["outcome"] == (outcome or "not_covered")
    assert view["items"][0]["revision"] != current["revision"]
    assert current["report"]["historical_rechecks"][0]["finding_id"] == finding["id"]
    # Restarting the service simply reads the same durable report.
    restarted = Analyses(execution, Engine()).get(current["id"])
    assert restarted["historical_review"] == view
    with execution.store.connection() as db:
        original = db.execute("SELECT * FROM analysis_findings WHERE id=?",
                              (finding["id"],)).fetchone()
        assert original["status"] == "confirmed" and original["version"] == 2
        assert json.loads(original["feedback"])["decision"] == "confirmed"
        assert db.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0] == 0
    assert analyses.get(old["id"])["findings"][0]["status"] == "confirmed"


def test_retry_reuses_original_historical_snapshot(history):
    execution, _, env, analyses, _, finding = history

    class FailOnce(Engine):
        contexts = []

        async def analyze(self, ctx, event):
            self.contexts.append(copy.deepcopy(ctx["historical_findings"]))
            if len(self.contexts) == 1:
                raise ValueError("fixture_failure")
            return await super().analyze(ctx, event)

    engine = FailOnce()
    analyses.engine = engine
    execution.action(env["id"], execution.get(env["id"])["version"], "test")
    execution.tick()
    asyncio.run(analyses.tick())
    failed = analyses.list(env["id"])[0]
    assert failed["status"] == "failed"
    with execution.store.connection() as db:
        db.execute("UPDATE analysis_findings SET version=version+1,status='not_reproduced' "
                   "WHERE id=?",
                   (finding["id"],))
    analyses.retry(failed["id"], failed["version"])
    asyncio.run(analyses.tick())
    assert engine.contexts[0] == engine.contexts[1]
    result = analyses.get(failed["id"])["report"]["historical_rechecks"][0]
    assert result["outcome"] == "not_covered"


@pytest.mark.parametrize("change", ["cancelled", "revision_changed"])
def test_late_historical_report_cannot_resurrect_cancelled_or_stale_analysis(history, change):
    execution, _, env, analyses, _, finding = history

    class LateEngine(Engine):
        async def analyze(self, ctx, event):
            item = recheck("fixed")
            item["finding_id"] = finding["id"]
            result = await run_analysis(HistoricalModel([item]), ctx, event)
            with execution.store.connection() as db:
                if change == "cancelled":
                    db.execute("UPDATE repository_analyses SET status='cancelled',"
                               "version=version+1 "
                               "WHERE env_id=? AND status='running'", (env["id"],))
                else:
                    db.execute("UPDATE environments SET revision=? WHERE id=?",
                               ("c" * 40, env["id"]))
            return result

    analyses.engine = LateEngine()
    execution.action(env["id"], execution.get(env["id"])["version"], "test")
    execution.tick()
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    assert row["status"] == ("cancelled" if change == "cancelled" else "failed")
    assert row["report"] is None and row["historical_review"] is None
    assert not row["findings"]
    with execution.store.connection() as db:
        assert db.execute("SELECT status FROM analysis_findings WHERE id=?",
                          (finding["id"],)).fetchone()[0] == "confirmed"
        assert db.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0] == 0


def test_historical_evidence_correction_is_bounded_and_keeps_origin_packet():
    class CorrectOnce(HistoricalModel):
        async def get_response(self, system_instructions, input, **kwargs):
            if self.calls < 6:
                self.items = [{**recheck("fixed"), "finding_id": "invented_id"}]
            else:
                self.items = [recheck("not_covered")]
            return await super().get_response(system_instructions, input, **kwargs)

    model = CorrectOnce([])
    events = []
    data = asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert data["historical_rechecks"][0]["outcome"] == "not_covered"
    assert model.calls == 7
    assert len([kind for kind, _ in events if kind == "evidence_retry"]) == 1
    packet = json.loads(model.inputs[-1][1])
    assert packet["targets"][0]["section"] == "historical_rechecks"
    packet = packet["evidence_data"]
    assert packet["run_context"]["historical_findings"]["items"][0]["finding_id"] == "finding_old"
    assert packet["execution_was_read"] is True
    assert packet["source_evidence"][0]["path"] == "app.py"
    with pytest.raises(AnalysisFailure, match="analysis_report_invalid"):
        asyncio.run(run_analysis(HistoricalModel([
            {**recheck("fixed"), "finding_id": "invented_id"},
        ]), context()))
