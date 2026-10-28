"""Offline recovery through the real SDK runner and durable analysis service."""

import asyncio
import copy
import json

import pytest
from agents import ModelResponse, ModelSettings, Usage
from openai.types.responses import ResponseFunctionToolCall
from test_execution import service  # noqa: F401
from test_repository_analysis import ReviewModel, prepare_tested

from repowatch.analysis_model import AnalysisModel, OutputTruncated
from repowatch.report_recovery import (
    checkpoint,
    digest,
    final_input,
    output_budget,
    valid_checkpoint,
)
from repowatch.repository_analysis import (
    ROLES,
    Analyses,
    AnalysisFailure,
    StageBudget,
    StageModel,
    run_analysis,
)
from repowatch.store import Conflict


def context():
    return {"files": {"app.py": "print('ok')\n# second\n# third\nUNREAD"},
            "config": {}, "jobs": [], "test_result": None}


def state(value):
    return checkpoint(value, {"app.py"}, {"app.py": [[1, 2], [2, 3], [1, 1]]},
                      [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])], True, [])


def test_output_budget_grows_with_scope_and_historical_items_and_stays_bounded():
    small = output_budget(context())
    scoped = output_budget({"task_scope": {"items": [{}] * 6}})
    medium = output_budget({"task_scope": {"items": [{}] * 6},
                            "historical_findings": {"items": [{}] * 8}})
    large = output_budget({"task_scope": {"items": [{}] * 6},
                           "historical_findings": {"items": [{}] * 200}})
    assert small == {"max_tokens": 5000, "reserve_seconds": 260, "review_items": 0}
    assert small["max_tokens"] < scoped["max_tokens"] < medium["max_tokens"] == 12800
    assert medium["max_tokens"] < large["max_tokens"] == 100000
    assert 240 < scoped["reserve_seconds"] <= large["reserve_seconds"] <= 360


def test_compaction_merges_only_read_lines_and_retains_complete_requirements():
    value = context()
    original = "Original request, including Trae Work CN. " * 50
    value["issue"] = {"body": original}
    value["task_scope"] = {"original": {"body": original}, "items": [{"key": "cn"}],
                           "execution_limits": ["Windows client not verified"]}
    packet = json.loads(final_input(value, state(value)))
    assert packet["source_evidence"] == [{"path": "app.py", "source_blocks": [
        {"start_line": 1, "end_line": 3, "text": "print('ok')\n# second\n# third"},
    ]}]
    assert "UNREAD" not in json.dumps(packet)
    reference = packet["run_context"]["issue"]["body"]
    assert reference == packet["run_context"]["task_scope"]["original"]["body"]
    assert packet["shared_texts"][reference["shared_text_ref"]] == original
    assert packet["run_context"]["task_scope"]["execution_limits"] == [
        "Windows client not verified",
    ]


def test_checkpoint_observations_are_bounded_and_clipping_is_explicit():
    value = context()
    transcript = [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": str(i) + "x" * 3000},
    ]} for i in range(16)]
    saved = checkpoint(value, {"app.py"}, {"app.py": [[1, 1]]},
                       state(value)["handoffs"], True, transcript)
    assert valid_checkpoint(saved, value, ROLES)
    assert saved["observations_clipped"]
    assert len(saved["observations"]) == 12
    assert all(len(text) == 2000 for text in saved["observations"])
    assert json.loads(final_input(value, saved))["observations_clipped"]


class RecoveryEngine:
    def __init__(self, fail=True, invented=False, fail_code="analysis_authentication_failed"):
        self.fail, self.invented, self.version = fail, invented, 1
        self.fail_code = fail_code
        self.requests = []

    def status(self):
        return {"enabled": True, "model": "offline-fixture", "version": self.version}

    async def analyze(self, value, event):
        model = ReviewModel(invented=self.invented)
        # The model fixture can produce a final report without issuing prerequisites
        # again when starting directly at the independent reviewer.
        if value.get("_review_resume"):
            model.calls = 5
        original = model.get_response

        async def respond(system_instructions, input, **kwargs):
            self.requests.append((input, kwargs))
            if kwargs.get("output_schema") is not None and self.fail:
                raise AnalysisFailure(self.fail_code)
            return await original(system_instructions, input, **kwargs)

        model.get_response = respond
        return await run_analysis(model, value, event, reasoning_effort="high")


def failed_analysis(execution_fixture, *, issue=False):
    execution, repo, _ = execution_fixture
    env = prepare_tested(execution_fixture)
    analyses = Analyses(execution, RecoveryEngine())
    request = None
    if issue:
        request = {"id": "issue_fixture", "number": 2, "title": "Original", "body": "CN too"}
        with execution.store.connection() as db:
            db.execute("INSERT INTO github_issues(id,repo_id,number,title,body,state,fetched_at) "
                       "VALUES(?,?,?,?,?,'open',0)",
                       (request["id"], repo["id"], 2, request["title"], request["body"]))
        # Preserve the ordinary execution-triggered analysis, as the real Issue
        # pipeline does, so periodic discovery cannot replace this queued Issue.
        analyses.engine.fail = False
        analyses.enqueue(env["id"])
        asyncio.run(analyses.tick())
        analyses.engine.fail = True
    identity = analyses.enqueue(env["id"], issue=request)
    asyncio.run(analyses.tick())
    result = analyses.get(identity)
    assert result["status"] == "failed", result["error"]
    assert result["report_recovery"]["available"]
    assert result["report"] is None and result["findings"] == []
    return analyses, result


def test_restart_and_retry_only_calls_reviewer_and_reuses_real_audit(service):  # noqa: F811
    previous, failed = failed_analysis(service)
    engine = RecoveryEngine(fail=False)
    restarted = Analyses(previous.execution, engine)
    restarted.retry(failed["id"], failed["version"])
    asyncio.run(restarted.tick())
    result = restarted.get(failed["id"])
    assert result["status"] == "completed", result["error"]
    assert len(engine.requests) == 1
    packet, kwargs = engine.requests[0]
    assert not kwargs["tools"] and kwargs["model_settings"].tool_choice == "none"
    assert kwargs["model_settings"].reasoning.effort == "high"
    assert json.loads(packet)["execution_was_read"]
    assert len(result["report"]["handoffs"]) == 3
    assert result["report"]["files_read"] == ["app.py"]
    assert result["report"]["read_ranges"] == {"app.py": [[1, 1]]}
    assert len(result["findings"]) == 1
    events = result["events"]
    start = max(i for i, item in enumerate(events) if item["kind"] == "attempt")
    assert {item["kind"] for item in events[start:]} >= {"report_resumed", "model_call"}
    assert not any(item["kind"] in ("source", "evidence", "handoff") for item in events[start:])
    assert not result["report_recovery"]["available"]
    assert "review_checkpoint" not in [item["kind"] for item in events]
    with restarted.store.connection() as db:
        assert not db.execute("SELECT * FROM analysis_review_checkpoints").fetchall()


@pytest.mark.parametrize("change", ["model", "context", "protocol", "corruption", "issue"])
def test_invalid_checkpoint_falls_back_to_real_full_flow(service, change):  # noqa: F811
    analyses, failed = failed_analysis(service, issue=True)
    engine = RecoveryEngine(fail=False)
    analyses = Analyses(analyses.execution, engine)
    with analyses.store.connection() as db:
        if change == "model":
            engine.version += 1
        elif change == "issue":
            db.execute("UPDATE github_issues SET body='Changed request',version=version+1")
        elif change == "context":
            row = db.execute("SELECT context FROM repository_analyses WHERE id=?",
                             (failed["id"],)).fetchone()
            value = json.loads(row[0])
            value["files"]["app.py"] += "\n# changed snapshot"
            db.execute("UPDATE repository_analyses SET context=? WHERE id=?",
                       (json.dumps(value), failed["id"]))
        else:
            row = db.execute("SELECT payload FROM analysis_review_checkpoints").fetchone()
            value = json.loads(row[0])
            value["protocol"] = 999
            db.execute("UPDATE analysis_review_checkpoints SET payload=?,payload_digest=?",
                       (json.dumps(value), digest(value) if change == "protocol" else "corrupt"))
    assert not analyses.get(failed["id"])["report_recovery"]["available"]
    analyses.retry(failed["id"], failed["version"])
    asyncio.run(analyses.tick())
    result = analyses.get(failed["id"])
    assert result["status"] == "completed", result["error"]
    assert len(engine.requests) == 6
    assert any(item["kind"] == "report_checkpoint_invalidated" for item in result["events"])
    assert sum(item["kind"] == "handoff" for item in result["events"]) == 6


def test_new_execution_cannot_resume_stale_review(service):  # noqa: F811
    analyses, failed = failed_analysis(service)
    env = analyses.execution.get(failed["env_id"])
    analyses.execution.action(env["id"], env["version"], "test")
    analyses.execution.tick()
    assert not analyses.get(failed["id"])["report_recovery"]["available"]
    with pytest.raises(Conflict, match="analysis_stale"):
        analyses.retry(failed["id"], failed["version"])


def test_resume_still_rejects_fabricated_evidence_and_persists_no_findings(service):  # noqa: F811
    analyses, failed = failed_analysis(service)
    engine = RecoveryEngine(fail=False, invented=True)
    analyses = Analyses(analyses.execution, engine)
    analyses.retry(failed["id"], failed["version"])
    asyncio.run(analyses.tick())
    result = analyses.get(failed["id"])
    assert result["status"] == "queued"
    assert result["automatic_recovery"]["state"] == "waiting"
    assert result["report"] is None and not result["findings"]
    assert len(engine.requests) == 2


def test_legacy_failure_without_checkpoint_restarts_full_flow(service):  # noqa: F811
    analyses, failed = failed_analysis(service)
    with analyses.store.connection() as db:
        db.execute("DELETE FROM analysis_review_checkpoints")
    engine = RecoveryEngine(fail=False)
    analyses = Analyses(analyses.execution, engine)
    assert not analyses.get(failed["id"])["report_recovery"]["available"]
    analyses.retry(failed["id"], failed["version"])
    asyncio.run(analyses.tick())
    result = analyses.get(failed["id"])
    assert result["status"] == "completed", result["error"]
    assert len(engine.requests) == 6
    assert not any(item["kind"] == "report_resumed" for item in result["events"])


def test_optional_scope_version_and_current_analysis_are_bound(service):  # noqa: F811
    analyses, failed = failed_analysis(service)
    with analyses.store.connection() as db:
        row = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                         (failed["id"],)).fetchone()
        value = json.loads(row["context"])
        scope = analyses.scope_basis(db, row)
        value["task_scope"] = {"id": scope["id"], "version": scope["version"],
                               "items": [{"key": "cn"}]}
        saved = state(value)
        saved["read_ranges"] = {"app.py": [[1, 1]]}
        binding = analyses.review_binding(db, row, value, analyses.model_identity())
        db.execute("UPDATE repository_analyses SET context=? WHERE id=?",
                   (json.dumps(value), row["id"]))
        db.execute("UPDATE analysis_review_checkpoints SET binding=?,payload=?,payload_digest=?",
                   (binding, json.dumps(saved), digest(saved)))
    assert analyses.get(failed["id"])["report_recovery"]["available"]
    with analyses.store.connection() as db:
        db.execute("UPDATE task_scopes SET version=version+1 WHERE id=?", (scope["id"],))
    assert not analyses.get(failed["id"])["report_recovery"]["available"]
    with analyses.store.connection() as db:
        db.execute(
            "INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,trigger_key,"
            "status,context,created,updated) SELECT 'other_analysis',env_id,repo_id,revision,"
            "fingerprint,'other_trigger',status,context,created,updated "
            "FROM repository_analyses WHERE id=?", (failed["id"],),
        )
        db.execute("UPDATE task_scopes SET version=?,analysis_id='other_analysis' WHERE id=?",
                   (scope["version"], scope["id"]))
    assert not analyses.get(failed["id"])["report_recovery"]["available"]


def test_model_identity_binds_key_rotation_and_redacts_checkpoint_echo(service):  # noqa: F811
    execution, _, _ = service
    settings = AnalysisModel(execution.store, execution.github)
    args = dict(version=0, enabled=True, model="offline-model", api="responses",
                base_url="https://provider.example/v1", api_key="synthetic-private-key-one")
    settings.save(**args)
    identity, events = settings.recovery_identity(), []

    async def runner(_model, _context, event, **_kwargs):
        event("review_checkpoint", json.dumps({"observations": [args["api_key"]]}))
        return {"summary": "fixture"}

    asyncio.run(settings._run(runner, {"_review_model_identity": identity},
                              lambda *event: events.append(event), flow="analysis"))
    assert args["api_key"] not in str(events)
    assert "[REDACTED]" in events[-1][1]
    settings.save(**{**args, "version": 1, "api_key": "synthetic-private-key-two"})
    assert settings.recovery_identity() != identity
    with pytest.raises(Conflict, match="analysis_model_changed"):
        asyncio.run(settings._run(runner, {"_review_model_identity": identity},
                                  lambda *_: None, flow="analysis"))


@pytest.mark.parametrize("change", ["cancel", "model"])
def test_change_during_resumed_call_cannot_commit_report(service, change):  # noqa: F811
    analyses, failed = failed_analysis(service)
    engine = RecoveryEngine(fail=False)
    analyses = Analyses(analyses.execution, engine)
    original = engine.analyze

    async def changed(value, event):
        def record(kind, data):
            event(kind, data)
            if kind == "report_budget":
                if change == "model":
                    engine.version += 1
                else:
                    with analyses.store.connection() as db:
                        db.execute("UPDATE repository_analyses SET status='cancelled',"
                                   "version=version+1 WHERE id=?", (failed["id"],))
        return await original(value, record)

    engine.analyze = changed
    analyses.retry(failed["id"], failed["version"])
    asyncio.run(analyses.tick())
    result = analyses.get(failed["id"])
    assert result["status"] in ("failed", "cancelled")
    assert result["report"] is None and result["findings"] == []
    with analyses.store.connection() as db:
        assert not db.execute("SELECT * FROM analysis_review_checkpoints").fetchall()


def test_reviewer_reads_refresh_checkpoint_before_the_next_request():
    value, events = context(), []
    model = ReviewModel()
    original = model.get_response
    reviews = 0

    async def respond(*args, **kwargs):
        nonlocal reviews
        if kwargs.get("output_schema") is not None:
            reviews += 1
            if reviews == 1:
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id="reviewer_read", call_id="reviewer_read", type="function_call",
                    name="read_source_file", arguments=json.dumps({
                        "path": "app.py", "start_line": 4, "end_line": 4,
                    }),
                )], usage=Usage(requests=1), response_id=None)
            raise AnalysisFailure("analysis_provider_timeout_exhausted")
        return await original(*args, **kwargs)

    model.get_response = respond
    with pytest.raises(AnalysisFailure, match="analysis_provider_timeout_exhausted"):
        asyncio.run(run_analysis(model, value, lambda *event: events.append(event)))
    saved = [json.loads(data) for kind, data in events if kind == "review_checkpoint"]
    assert len(saved) == 2
    assert saved[0]["read_ranges"] == {"app.py": [[1, 3]]}
    assert saved[1]["read_ranges"] == {"app.py": [[1, 3], [4, 4]]}
    assert valid_checkpoint(saved[1], value, ROLES)
    assert sum(kind == "source" for kind, _ in events) == 2


@pytest.mark.parametrize("error,items,initial,limit", [
    (OutputTruncated, 6, 7600, 15200),
    (TimeoutError, 6, 7600, 7600),
])
def test_finalization_retry_uses_compact_input_and_bounded_budget(
    monkeypatch, error, items, initial, limit,
):
    monkeypatch.setattr("repowatch.repository_analysis.RETRY_DELAY_SECONDS", 0)
    value, calls, events = context(), [], []
    value["task_scope"] = {"items": [{"key": f"item_{i}"} for i in range(items)]}
    model = ReviewModel()
    original = model.get_response

    async def respond(*args, **kwargs):
        if kwargs.get("output_schema") is not None:
            calls.append(kwargs)
            if len(calls) == 1:
                raise error("fixture finalization failure")
        return await original(*args, **kwargs)

    model.get_response = respond
    report = asyncio.run(run_analysis(model, value, lambda *event: events.append(event)))
    assert len(calls) == 2 and len(report["handoffs"]) == 3
    assert calls[0]["model_settings"].max_tokens == initial
    assert calls[1]["model_settings"].max_tokens == limit
    assert calls[1]["model_settings"].tool_choice == "none" and not calls[1]["tools"]
    packet = json.loads(calls[1]["input"])
    assert len(packet["run_context"]["task_scope"]["items"]) == items
    assert any(kind == "review_checkpoint" for kind, _ in events)


def test_large_output_retry_is_capped_without_oversizing_the_task_scope():
    model = ReviewModel()
    model.calls = 5
    original = model.get_response
    requested = []

    async def respond(*args, **kwargs):
        requested.append(kwargs["model_settings"].max_tokens)
        if len(requested) == 1:
            raise OutputTruncated("fixture finalization failure")
        return await original(*args, **kwargs)

    model.get_response = respond
    stage = StageModel(model, ROLES[-1], StageBudget(), lambda *_: None)
    result = asyncio.run(stage.get_response(
        "Trusted final-review instructions", json.dumps(context()),
        tools=[], handoffs=[], output_schema=None,
        model_settings=ModelSettings(max_tokens=60000),
    ))
    assert result.output
    assert requested == [60000, 100000]


@pytest.mark.parametrize("field,value", [
    ("handoffs", []), ("execution_read", False), ("read_ranges", {"app.py": [[1, 10]]}),
    ("files_read", ["missing.py"]), ("observations", ["x" * 2001]),
    ("read_ranges", ["app.py"]), ("protocol", True),
])
def test_checkpoint_validation_rejects_missing_or_impossible_audit(field, value):
    original = context()
    saved = copy.deepcopy(state(original))
    saved[field] = value
    assert not valid_checkpoint(saved, original, ROLES)
