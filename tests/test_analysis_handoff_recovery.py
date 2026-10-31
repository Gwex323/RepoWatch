"""A premature text answer must not silently skip native review stages."""

import asyncio
import json

import pytest
from agents import ModelResponse, Usage
from agents.exceptions import ModelRefusalError
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal, ResponseOutputText
from test_execution import service  # noqa: F401
from test_repository_analysis import Engine, ReviewModel, prepare_tested

from repowatch.repository_analysis import (
    MAX_TURNS,
    ROLES,
    Analyses,
    AnalysisFailure,
    failure_code,
    run_analysis,
)

NOTE = "Early role observations are not an independent review."


def context():
    return {"files": {"app.py": "print('ok')"}, "config": {}, "jobs": [], "test_result": None}


class EarlyAnswerModel(ReviewModel):
    def __init__(self, roles, *, ignore_forced=False, refuse=False, invented=False):
        super().__init__(findings=invented, invented=invented)
        self.early = {f"transfer_to_{ROLES[ROLES.index(role) + 1]}" for role in roles}
        self.ignore_forced, self.refuse = ignore_forced, refuse
        self.emitted = set()
        self.requests = []
        self.recoveries = []

    async def get_response(self, system_instructions, input, **kwargs):
        self.requests.append(kwargs)
        next_tool = kwargs["handoffs"][0].tool_name if kwargs["handoffs"] else None
        choice = kwargs["model_settings"].tool_choice
        if choice == next_tool and next_tool in self.emitted:
            self.recoveries.append((system_instructions, input, kwargs))
        if (next_tool in self.early and self.calls >= 2 and
                (self.ignore_forced or next_tool not in self.emitted) and
                (self.ignore_forced or choice != next_tool)):
            self.emitted.add(next_tool)
            if self.ignore_forced and len(self.recoveries) > 2:
                raise AnalysisFailure("analysis_tool_arguments_exhausted")
            self.calls += 1
            content = (ResponseOutputRefusal(type="refusal", refusal="Fixture provider refusal.")
                       if self.refuse else ResponseOutputText(
                           type="output_text", text=NOTE, annotations=[],
                       ))
            return ModelResponse(output=[ResponseOutputMessage(
                id=f"early_{self.calls}", type="message", role="assistant", status="completed",
                content=[content],
            )], usage=Usage(requests=1), response_id=None)
        return await super().get_response(system_instructions, input, **kwargs)


@pytest.mark.parametrize("roles", [
    [ROLES[0]], [ROLES[1]], [ROLES[2]], list(ROLES[:-1]),
])
def test_each_nonfinal_role_can_finish_early_then_perform_one_real_handoff(roles):
    model, events = EarlyAnswerModel(roles), []
    result = asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert result["handoffs"] == [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])]
    assert result["execution_read"] and result["files_read"] == ["app.py"]
    assert events.count(("evidence", "execution_records")) == 1
    assert events.count(("source", "app.py")) == 1
    assert [role for kind, role in events if kind == "handoff_recovery"] == roles
    assert len(model.recoveries) == len(roles)
    assert model.calls == 6 + len(roles) and MAX_TURNS is None
    for instructions, input, kwargs in model.recoveries:
        assert kwargs["tools"] == []
        assert kwargs["model_settings"].tool_choice == kwargs["handoffs"][0].tool_name
        assert kwargs["model_settings"].parallel_tool_calls is False
        assert "Exploration is closed" in instructions
        assert NOTE in json.dumps(input)
        assert any(item.get("type") == "function_call_output" for item in input)
        assert sum(item.get("type") == "function_call" and
                   item.get("name") == "read_source_file" for item in input) == 1
        assert input[-1]["role"] == "user"
        assert "SAME review" in input[-1]["content"]
        assert "Later roles may continue inspecting needed evidence" in input[-1]["content"]
    role, calls = None, {name: 0 for name in ROLES}
    for kind, name in events:
        if kind == "agent":
            role = name
        elif kind == "model_call":
            calls[role] += 1
    assert all(calls[role] >= 1 for role in ROLES)


def test_ignored_recovery_fails_once_without_faking_missing_handoffs():
    model, events = EarlyAnswerModel([ROLES[1]], ignore_forced=True), []
    with pytest.raises(AnalysisFailure, match="analysis_tool_arguments_exhausted"):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert [name for kind, name in events if kind == "handoff"] == [f"{ROLES[0]} -> {ROLES[1]}"]
    assert events.count(("handoff_recovery", ROLES[1])) == 1
    assert len(model.recoveries) == 3 and model.calls >= 6
    assert events.count(("source", "app.py")) == 1
    assert events.count(("evidence", "execution_records")) == 1


def test_refusal_is_not_reinterpreted_as_permission_to_continue():
    model, events = EarlyAnswerModel([ROLES[1]], refuse=True), []
    with pytest.raises((AnalysisFailure, ModelRefusalError)) as exc:
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert failure_code(exc.value) == "analysis_model_refused"
    assert not any(kind == "handoff_recovery" for kind, _ in events)
    assert model.calls == 4


def test_recovery_does_not_relax_final_current_source_quote_validation():
    model, events = EarlyAnswerModel([ROLES[1]], invented=True), []
    with pytest.raises(AnalysisFailure, match="analysis_report_invalid"):
        asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert events.count(("handoff_recovery", ROLES[1])) == 1
    assert sum(kind == "handoff" for kind, _ in events) == 3
    assert events.count(("source", "app.py")) == 1


def test_early_role_recovery_persists_only_after_independent_review(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)

    class RecoveryEngine(Engine):
        async def analyze(self, context, event):
            return await run_analysis(EarlyAnswerModel([ROLES[1]]), context, event)

    analyses = Analyses(execution, RecoveryEngine())
    asyncio.run(analyses.tick())
    completed = analyses.list(env["id"])[0]
    assert completed["status"] == "completed" and completed["error"] is None
    assert completed["findings"] == []
    assert len(completed["report"]["handoffs"]) == 3
    assert sum(item["kind"] == "attempt" for item in completed["events"]) == 1
    assert sum(item["kind"] == "handoff_recovery" for item in completed["events"]) == 1
    assert completed["progress"]["model_calls"] == 8  # Includes durable scope registration.
    assert completed["progress"]["source_reads"] == 1
    with execution.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0] == 0


def test_cancel_after_early_stop_cannot_publish_late_recovered_report(service):  # noqa: F811
    execution, _, _ = service
    env = prepare_tested(service)

    class CancellingEngine(Engine):
        async def analyze(self, context, event):
            def record(kind, value):
                result = event(kind, value)
                if kind == "handoff_recovery":
                    with execution.store.connection() as db:
                        db.execute("UPDATE repository_analyses SET status='cancelled',"
                                   "version=version+1 WHERE env_id=? AND status='running'",
                                   (env["id"],))
                return result
            return await run_analysis(EarlyAnswerModel([ROLES[1]]), context, record)

    analyses = Analyses(execution, CancellingEngine())
    asyncio.run(analyses.tick())
    row = analyses.list(env["id"])[0]
    assert row["status"] == "cancelled" and row["report"] is None
    assert row["findings"] == [] and row["historical_review"] is None


def test_recovery_has_no_overall_runner_turn_limit(monkeypatch):
    import repowatch.repository_analysis as module

    assert module.MAX_TURNS is None
    model, events = EarlyAnswerModel([ROLES[1]]), []
    report = asyncio.run(run_analysis(model, context(), lambda *args: events.append(args)))
    assert model.calls > 4 and model.recoveries
    assert len(report["handoffs"]) == 3
    assert events.count(("source", "app.py")) == 1
