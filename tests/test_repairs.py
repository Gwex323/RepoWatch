"""Synthetic repair evidence only: never call a live model or publish a real contribution."""

import ast
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import Model, ModelResponse, ModelSettings, Usage
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.shared import Reasoning
from test_execution import Runtime, Source, service  # noqa: F401
from test_github_accounts import TOKEN_A, MemoryVault, Remote
from test_repository_analysis import Engine, prepare_tested

from repowatch.analysis_model import AnalysisModel, OutputTruncated
from repowatch.app import create_app
from repowatch.github_accounts import GitHubError
from repowatch.repair_model import (
    RepairStageModel,
    patch_arguments,
    repair_failure_code,
    repair_failure_details,
    run_repair,
)
from repowatch.repair_workspace import (
    Patch,
    RepairWorkspace,
    SourceContentBlocked,
    SourceReadUnavailable,
    digest,
    safe_path,
)
from repowatch.repair_workspace import test_path as is_test
from repowatch.repairs import Repairs
from repowatch.repository_analysis import Analyses
from repowatch.store import Conflict


def fixture_patch():
    return {
        "summary": "Synthetic patch; not a real repository fix.",
        "edits": [
            {"path": "app.py", "original_sha256": digest(b"print('ok')"),
             "content": "print('fixed')\n"},
            {"path": "tests/test_repowatch.py", "original_sha256": "",
             "content": "assert True\n"},
        ],
        "regression_paths": ["tests/test_repowatch.py"],
        "regression_command": "python -m unittest discover -s tests",
    }


class RepairRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.regression_before = 1
        self.regression_after = 0
        self.full_after = 0
        self.test_calls = []
        self.removed_volumes = []
        self.setup_code = 0
        self.log = "1 test passed"

    def run(self, env, action, check):
        if not env["id"].startswith("repair_"):
            return super().run(env, action, check)
        check()
        self.test_calls.append((env["id"], action, env["config"]["test"]))
        if action == "setup":
            return self.setup_code, "fixture setup log"
        if env["id"].endswith("-1"):
            return self.regression_before, "1 test failed for expected fixture assertion"
        if env["id"].endswith("-2"):
            previous = [x for x in self.test_calls if x[0] == env["id"] and x[1] == "test"]
            return (self.regression_after if len(previous) == 1 else self.full_after), self.log
        return 0, self.log

    def cleanup(self, env):
        self.removed_volumes.append(env["id"])


class RepairModel(Model):
    def __init__(self, *, approved=True, skip_handoff=False):
        self.calls, self.requests = 0, []
        self.approved, self.skip_handoff = approved, skip_handoff

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        name, args = None, {}
        if self.calls == 1:
            name, args = "read_repair_source", {"path": "app.py", "candidate": False}
        elif self.calls == 2:
            name, args = "submit_and_test_repair", {"patch": fixture_patch()}
        elif self.calls == 3 and not self.skip_handoff:
            name = kwargs["handoffs"][0].tool_name
        elif self.calls == 4:
            name = "read_repair_verification"
        if name:
            output = ResponseFunctionToolCall(
                id=f"call_{self.calls}", call_id=f"call_{self.calls}", type="function_call",
                name=name, arguments=json.dumps(args),
            )
        else:
            output = ResponseOutputMessage(
                id=f"response_{self.calls}", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(
                    type="output_text", annotations=[], text=json.dumps({
                        "approved": self.approved, "summary": "Synthetic independent review.",
                        "risks": ["Mock execution is not live validation."],
                    }),
                )],
            )
        return ModelResponse(output=[output], usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError()
        yield


class RepairEngine:
    enabled = True

    def __init__(self, approved=True):
        self.models = []
        self.approved = approved

    def status(self):
        return {"enabled": self.enabled, "model": "synthetic-repair"}

    async def repair(self, context, event, *, workspace):
        model = RepairModel(approved=self.approved)
        self.models.append(model)
        return await run_repair(model, context, event, workspace=workspace)


class Publisher:
    def __init__(self):
        self.calls = []
        self.fail = False

    def publish(self, task, patch, body, *, check):
        check()
        self.calls.append((task, patch, body))
        if self.fail:
            raise GitHubError("github_unreachable", 502)
        return {
            "repository": "alice/project", "branch": "repowatch/synthetic", "commit": "c" * 40,
            "pr": {"number": 2, "url": "https://github.com/alice/project/pull/2"},
            "issue": {"number": 1, "url": "https://github.com/alice/project/issues/1"},
        }


@pytest.fixture
def pending(service):  # noqa: F811
    execution, repo, _ = service
    runtime = RepairRuntime()
    execution.runtime = runtime
    env = prepare_tested((execution, repo, runtime))
    analyses = Analyses(execution, Engine())
    asyncio.run(analyses.tick())
    report = analyses.list(env["id"])[0]
    engine, publisher = RepairEngine(), Publisher()
    repairs = Repairs(execution, engine, analyses=analyses, publisher=publisher)
    analyses.repairer = repairs
    return repairs, analyses, report, runtime, publisher


def confirm(pending):
    repairs, analyses, report, _, _ = pending
    finding = report["findings"][0]
    analyses.feedback(report["id"], finding["id"], finding["version"], "confirmed", "", True)
    return repairs.list(report["env_id"])[0]


def enable(repairs):
    settings = repairs.settings()
    return repairs.configure(settings["version"], True)


def test_feedback_atomically_queues_one_repair_and_upgrade_backfills_without_reconfirmation(
    pending,
):
    repairs, analyses, report, _, _ = pending
    analyses.repairer = None
    finding = report["findings"][0]
    analyses.feedback(report["id"], finding["id"], 1, "confirmed", "", True)
    assert repairs.list(report["env_id"]) == []
    repairs.backfill()
    original = repairs.list(report["env_id"])[0]
    repairs.backfill()
    assert len(repairs.list(report["env_id"])) == 1
    assert repairs.enqueue(report["id"], finding["id"]) == original["id"]
    assert original["context"]["confirmation"]["note"] == ""
    assert original["status"] == "queued" and not original["enabled"]
    assert not asyncio.run(repairs.tick())
    assert repairs.engine.models == []


def test_negative_or_stale_finding_never_queues_repairs(pending):
    repairs, analyses, report, _, _ = pending
    finding = report["findings"][0]
    analyses.feedback(report["id"], finding["id"], 1, "not_reproduced", "", False)
    with pytest.raises(Conflict):
        repairs.enqueue(report["id"], finding["id"])
    repairs.backfill()
    assert repairs.list(report["env_id"]) == []


def test_confirmed_finding_runs_real_sdk_tools_and_handoff_then_publishes_once(pending):
    repairs, analyses, report, runtime, publisher = pending
    queued = confirm(pending)
    enable(repairs)
    original = repairs.execution.directory(report["env_id"]) / "source" / "app.py"
    assert asyncio.run(repairs.tick())
    task = repairs.get(queued["id"])
    assert task["status"] == "pr_created"
    assert task["review"]["approved"] is True and task["verification"]["passed"] is True
    assert [event["value"] for event in task["events"] if event["kind"] == "handoff"] == [
        "repair_agent -> independent_reviewer",
    ]
    requests = repairs.engine.models[0].requests
    assert not requests[0]["handoffs"] and not requests[1]["handoffs"]
    assert requests[2]["handoffs"][0].tool_name == "transfer_to_independent_reviewer"
    assert original.read_text() == "print('ok')"
    assert len(publisher.calls) == 1
    assert len(runtime.removed_volumes) >= 6
    assert [key for key, value in task["verification"].items() if isinstance(value, dict)] == [
        "original_suite_setup", "original_suite", "baseline_setup", "regression_before",
        "candidate_setup", "regression_after", "full_suite_after",
    ]
    assert not asyncio.run(repairs.tick())
    assert len(repairs.engine.models) == 1
    # Source-analysis jobs were not fabricated by repair tests.
    assert len(analyses.list(report["env_id"])) == 1


@pytest.mark.parametrize("field,value,error", [
    ("regression_before", 0, "repair_tests_failed"),
    ("regression_before", 5, "repair_tests_failed"),
    ("regression_after", 1, "repair_tests_failed"),
    ("full_after", 1, "repair_tests_failed"),
    ("setup_code", 1, "repair_setup_failed"),
    ("log", "Ran 0 tests", "repair_tests_failed"),
])
def test_failed_or_empty_verification_never_publishes(pending, field, value, error):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    setattr(runtime, field, value)
    assert asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "failed" and result["error"] == error
    assert result["patch"] is not None and result["verification"] is not None
    assert publisher.calls == []


def test_independent_review_veto_blocks_publication_and_preserves_diff(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    repairs.engine.approved = False
    enable(repairs)
    assert asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_review_rejected"
    assert result["review"]["approved"] is False and result["diff"]
    assert not publisher.calls


@pytest.mark.parametrize("failure", ["review", "tests", "setup"])
def test_retry_unverified_attempt_reexecutes_and_preserves_prior_artifacts(pending, failure):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    if failure == "review":
        repairs.engine.approved = False
    elif failure == "tests":
        runtime.full_after = 1
    else:
        runtime.setup_code = 1
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    assert failed["status"] == "failed" and failed["patch"]
    retried = repairs.retry(task["id"], failed["version"])
    assert retried["patch"] is retried["review"] is retried["verification"] is None
    assert retried["attempts"][0]["diff"] == failed["diff"]
    assert retried["attempts"][0]["error"] == failed["error"]
    repairs.engine.approved = True
    runtime.full_after = runtime.setup_code = 0
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "pr_created"
    assert len(repairs.engine.models) == 2 and len(publisher.calls) == 1


def test_docker_preflight_prevents_model_calls_and_offline_retry_does_not_consume_attempt(pending):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    runtime.fail = True
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    assert failed["error"] == "docker_unavailable" and not failed["patch"]
    assert not repairs.engine.models and not runtime.test_calls and not publisher.calls
    with pytest.raises(Conflict, match="docker_unavailable"):
        repairs.retry(task["id"], failed["version"])
    unchanged = repairs.get(task["id"])
    assert unchanged["attempt"] == failed["attempt"]
    assert unchanged["version"] == failed["version"] and not unchanged["attempts"]
    runtime.fail = False
    repairs.retry(task["id"], failed["version"])
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "pr_created" and len(publisher.calls) == 1


@pytest.mark.parametrize("outcome", ["passed", "tests_failed", "source_changed", "account_changed"])
def test_docker_recovery_retains_patch_and_revalidates_before_native_review(pending, outcome):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    readiness_calls = []

    def readiness():
        readiness_calls.append(True)
        return {"ready": len(readiness_calls) == 1, "reason": "docker_unavailable"}

    runtime.readiness = readiness
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    assert failed["error"] == "docker_unavailable" and failed["patch"]
    assert failed["verification_resume_available"] and failed["verification"] is None
    assert not runtime.test_calls and not publisher.calls
    with pytest.raises(Conflict, match="docker_unavailable"):
        repairs.retry(task["id"], failed["version"])
    runtime.readiness = lambda: {"ready": True, "reason": ""}
    if outcome == "account_changed":
        with repairs.store.connection() as db:
            db.execute("UPDATE github_configuration SET active_account_id=NULL")
        with pytest.raises(Conflict, match="repair_account_changed"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["patch"] == failed["patch"] and queued["diff"] == failed["diff"]
    assert queued["attempts"][0]["patch"] == failed["patch"]
    assert queued["context"]["resume_verification"] is True
    models = []

    async def review(context, event, *, workspace):
        assert workspace.verification["passed"]
        model = RepairModel()
        model.calls = 2  # A retained verified patch starts at native handoff.
        models.append(model)
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = review
    if outcome == "tests_failed":
        runtime.full_after = 1
    if outcome == "source_changed":
        source = repairs.execution.directory(task["env_id"]) / "source/app.py"
        source.write_text("print('changed')\n")
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    if outcome == "passed":
        assert final["status"] == "pr_created" and len(publisher.calls) == 1
        assert len(models) == 1 and len(models[0].requests) == 3
        assert models[0].requests[0]["model_settings"].tool_choice == (
            "transfer_to_independent_reviewer"
        )
        assert len(runtime.test_calls) == 7
        assert final["patch"] == failed["patch"]
    else:
        assert final["error"] == (
            "repair_tests_failed" if outcome == "tests_failed" else "repair_source_changed"
        )
        assert not models and not publisher.calls


def test_expired_lease_recovers_even_with_disabled_model_and_paused_queue(pending):
    repairs, _, _, _, _ = pending
    task = confirm(pending)
    with repairs.store.connection() as db:
        db.execute(
            "UPDATE repair_tasks SET status='running',lease=?,owner='old-worker' WHERE id=?",
            (time.time() - 1, task["id"]),
        )
    repairs.engine.enabled = False
    assert not asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "interrupted" and result["can_retry"]
    assert not repairs.engine.models


def test_publication_retry_allows_same_identity_credential_refresh_not_account_switch(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    publisher.fail = True
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    old_version = failed["context"]["account_version"]
    account_id = failed["context"]["account_id"]
    repairs.execution.github.connect("alice", TOKEN_A)
    retried = repairs.retry(task["id"], failed["version"])
    assert retried["context"]["account_id"] == account_id
    assert retried["context"]["account_version"] > old_version
    publisher.fail = False
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "pr_created"
    assert len(repairs.engine.models) == 1


def test_verified_retry_never_silently_rebinds_account(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    publisher.fail = True
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    with repairs.store.connection() as db:
        db.execute("UPDATE github_configuration SET active_account_id=NULL")
    with pytest.raises(Conflict, match="repair_account_changed"):
        repairs.retry(task["id"], failed["version"])
    assert repairs.get(task["id"])["status"] == "failed"


def test_native_handoff_cannot_be_replaced_by_final_success_text(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)

    async def incomplete(context, event, *, workspace):
        return await run_repair(RepairModel(skip_handoff=True), context, event, workspace=workspace)

    repairs.engine.repair = incomplete
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_handoff_incomplete"
    assert not publisher.calls


def test_tool_failure_unwraps_only_safe_error_codes(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)

    class InvalidModel(RepairModel):
        async def get_response(self, *args, **kwargs):
            result = await super().get_response(*args, **kwargs)
            if self.calls == 1:
                result.output[0].arguments = json.dumps({"path": "../app.py"})
            return result

    async def invalid(context, event, *, workspace):
        return await run_repair(InvalidModel(), context, event, workspace=workspace)

    repairs.engine.repair = invalid
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["error"] == "repair_unsafe_path"
    assert Repairs._error_code(RuntimeError("secret provider response"), None) == "repair_failed"
    assert not publisher.calls


def test_cleanup_failure_does_not_discard_failed_setup_evidence(pending):
    repairs, _, report, runtime, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "b" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    workspace.read("app.py")
    workspace.prepare(Patch.model_validate(fixture_patch()))
    runtime.setup_code = 1
    count = 0

    def cleanup(env):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("cleanup failed")

    runtime.cleanup = cleanup
    with pytest.raises(Conflict, match="repair_setup_failed"):
        workspace.verify(lambda *args: None)
    assert workspace.verification["original_suite_setup"]["exit_code"] == 1


@pytest.mark.parametrize("correct", [True, False])
def test_review_format_correction_is_bounded_and_cannot_publish_invalid_review(pending, correct):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = RepairModel()
    delegate = model.get_response

    async def malformed(*args, **kwargs):
        result = await delegate(*args, **kwargs)
        if model.calls == 5 or not correct and model.calls > 5:
            result.output[0].content[0].text = '{"approved": "true"}'
        return result

    model.get_response = malformed

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert sum(e["kind"] == "format_retry" for e in result["events"]) == 1
    assert model.calls == 6
    if correct:
        assert result["status"] == "pr_created" and len(publisher.calls) == 1
    else:
        assert result["error"] == "repair_output_invalid" and not publisher.calls


@pytest.mark.parametrize("role,turns", [("repair_agent", 7), ("independent_reviewer", 4)])
def test_model_stage_budget_prevents_unbounded_provider_calls(pending, role, turns):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "e" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    delegate = RepairModel()
    model = RepairStageModel(delegate, role, workspace, lambda *args: None, dict, {"timeouts": 0})
    model.turns = turns
    with pytest.raises(Conflict, match="repair_model_budget"):
        asyncio.run(model.get_response("", [], handoffs=[], model_settings=ModelSettings()))
    assert delegate.calls == 0


@pytest.mark.parametrize("persistent", [False, True])
def test_truncated_repair_never_executes_partial_tools_and_preserves_handoff(pending, persistent):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model, requests = RepairModel(), []
    delegate = model.get_response

    async def truncated(system_instructions, input, **kwargs):
        requests.append((system_instructions, kwargs["model_settings"].max_tokens,
                         kwargs["model_settings"].reasoning.effort))
        if len(requests) == 2 or persistent and len(requests) == 3:
            raise OutputTruncated("private-partial-provider-output")
        return await delegate(system_instructions, input, **kwargs)

    model.get_response = truncated

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace, reasoning_effort="high")

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert sum(e["kind"] == "output_retry" for e in result["events"]) == 1
    assert requests[1][1] == 12000 and requests[2][1] == 16000
    assert "previous response exhausted its output budget" in requests[2][0]
    assert requests[1][2] == "high" and requests[2][2] == "low"
    assert "private-partial-provider-output" not in json.dumps(result)
    if persistent:
        assert len(requests) == 3 and result["error"] == "repair_output_truncated"
        assert not result["patch"] and not runtime.test_calls and not publisher.calls
    else:
        assert result["status"] == "pr_created" and len(publisher.calls) == 1
        assert requests[3][2] == "low" and requests[-1][2] == "high"
        assert [e["value"] for e in result["events"] if e["kind"] == "handoff"] == [
            "repair_agent -> independent_reviewer",
        ]


def test_output_retries_share_a_run_budget(pending):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "e" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    delegate = RepairModel()

    async def truncated(*args, **kwargs):
        delegate.calls += 1
        if delegate.calls in (1, 3, 5):
            raise OutputTruncated("private-provider-output")
        return SimpleNamespace(output=[])

    delegate.get_response = truncated
    budget = {"timeouts": 0, "outputs": 0}
    for count in range(3):
        model = RepairStageModel(delegate, "repair_agent", workspace, lambda *_: None, dict,
                                 budget)
        call = model.get_response("", [], handoffs=[], model_settings=ModelSettings())
        if count == 2:
            with pytest.raises(Conflict, match="repair_output_truncated"):
                asyncio.run(call)
        else:
            asyncio.run(call)
    assert delegate.calls == 5 and budget["outputs"] == 2
    assert repair_failure_code(OutputTruncated("private")) == "repair_output_truncated"


def test_model_timeouts_have_one_retry_and_respect_shared_budget(pending):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "f" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    delegate = RepairModel()

    async def timeout(*args, **kwargs):
        delegate.calls += 1
        raise TimeoutError("untrusted provider body")

    delegate.get_response = timeout
    budget = {"timeouts": 0}
    for expected_calls, used_budget in [(2, 1), (4, 2), (5, 2)]:
        model = RepairStageModel(
            delegate, "repair_agent", workspace, lambda *args: None, dict, budget,
        )
        with pytest.raises(Conflict, match="repair_model_timeout"):
            asyncio.run(model.get_response("", [], handoffs=[], model_settings=ModelSettings()))
        assert delegate.calls == expected_calls
        assert budget["timeouts"] == used_budget


@pytest.mark.parametrize("effort,expected", [("auto", "low"), ("default", None), ("high", "high")])
def test_repair_sdk_transport_uses_extended_timeout_without_changing_analysis(
    pending, monkeypatch, effort, expected,
):
    import httpx
    from agents.models.interface import ModelTracing
    from openai.types.shared import Reasoning

    repairs, _, _, _, _ = pending
    engine = AnalysisModel(repairs.store, repairs.execution.github)
    engine.save(version=0, enabled=True, model="glm-5.3-flash", api="chat_completions",
                base_url="https://provider.example/v1", api_key="synthetic-model-key",
                reasoning_effort=effort)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "completion", "object": "chat.completion", "created": 0, "model": "synthetic",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
        })

    original_client = httpx.AsyncClient

    class Client(original_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(respond))

    async def runner(model, context, event, **kwargs):
        await model.get_response(
            system_instructions="Fixture", input="fixture",
            model_settings=ModelSettings(reasoning=Reasoning(effort=kwargs["reasoning_effort"])
                                         if kwargs["reasoning_effort"] else None),
            tools=[], handoffs=[], output_schema=None, tracing=ModelTracing.DISABLED,
        )
        assert model.delegate._client.max_retries == 0
        return {}

    monkeypatch.setattr("repowatch.analysis_model.httpx.AsyncClient", Client)
    monkeypatch.setattr("repowatch.repair_model.run_repair", runner)
    monkeypatch.setattr("repowatch.repository_analysis.run_analysis", runner)
    asyncio.run(engine.repair({}, lambda *_: None, workspace=None))
    asyncio.run(engine.analyze({}, lambda *_: None))
    assert [r.extensions["timeout"]["read"] for r in requests] == [450, 120]
    assert all(r.extensions["timeout"]["connect"] == 10 for r in requests)
    assert all(json.loads(r.content).get("reasoning_effort") == expected for r in requests)
    assert engine.status()["version"] == 1
    assert engine.status()["reasoning_effort"] == effort


def test_slow_repair_reply_can_exceed_old_timeout_and_emits_safe_timing(pending, monkeypatch):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "f" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    events, limits = [], []
    wait_for = asyncio.wait_for

    async def scaled_wait(call, timeout):
        limits.append(timeout)
        return await wait_for(call, timeout=timeout / 1000)

    delegate = RepairModel()
    response = delegate.get_response

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)  # Between the scaled old 120s and new 450s limits.
        return await response(*args, **kwargs)

    delegate.get_response = slow
    monkeypatch.setattr("repowatch.repair_model.asyncio.wait_for", scaled_wait)
    model = RepairStageModel(delegate, "repair_agent", workspace,
                            lambda *args: events.append(args), dict, {"timeouts": 0})
    asyncio.run(model.get_response("", [], handoffs=[], model_settings=ModelSettings()))
    assert limits == [450] and delegate.calls == 1
    assert [kind for kind, _ in events] == ["model_call", "request_started", "request_completed"]
    assert json.loads(events[-1][1])["elapsed_seconds"] >= 0


def test_real_wait_timeout_cancels_requests_and_never_logs_provider_text(pending, monkeypatch):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "f" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    cancelled, events = [], []
    delegate = RepairModel()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    delegate.get_response = hang
    monkeypatch.setattr("repowatch.repair_model.REPAIR_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("repowatch.repair_model.RETRY_DELAY_SECONDS", 0)
    model = RepairStageModel(delegate, "repair_agent", workspace,
                            lambda *args: events.append(args), dict, {"timeouts": 0})
    with pytest.raises(Conflict, match="repair_model_timeout"):
        asyncio.run(model.get_response("private prompt", [], handoffs=[],
                                       model_settings=ModelSettings()))
    assert len(cancelled) == 2
    assert sum(kind == "request_timeout" for kind, _ in events) == 2
    assert "private prompt" not in json.dumps(events)


def test_repair_overall_deadline_still_cancels_inflight_model(pending, monkeypatch):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    elapsed, cancelled = [0], []

    async def hang(*args, **kwargs):
        elapsed[0] = 1801
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    repairs.engine.repair = hang
    monkeypatch.setattr("repowatch.repairs.time", SimpleNamespace(
        monotonic=lambda: elapsed[0], time=time.time,
    ))
    asyncio.run(repairs.tick())
    assert cancelled == [True]
    assert repairs.get(task["id"])["error"] == "repair_deadline_exceeded"
    assert not publisher.calls and not runtime.test_calls


@pytest.mark.parametrize("old_policy,error,stale,allowed", [
    (True, "repair_model_timeout", False, True),
    (False, "repair_model_timeout", False, False),
    (True, "repair_tests_failed", False, False),
    (True, "repair_model_timeout", True, False),
])
def test_timeout_upgrade_recovery_is_explicit_versioned_and_one_time(
    pending, old_policy, error, stale, allowed,
):
    from repowatch.repairs import REPAIR_REQUEST_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = dict(task["context"])
    if not old_policy:
        context["request_policy"] = REPAIR_REQUEST_POLICY
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=3,error=?,context=? WHERE id=?",
                   (error, json.dumps(context), task["id"]))
        if stale:
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is allowed
    assert not asyncio.run(repairs.tick())  # Recovery cannot silently dispatch a failed task.
    if not allowed:
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    retried = repairs.retry(task["id"], failed["version"])
    assert retried["attempt"] == 3 and retried["status"] == "queued"
    assert retried["attempts"][0]["attempt"] == 3
    assert retried["attempts"][0]["error"] == error
    assert any(event["kind"] == "timeout_recovery" for event in retried["events"])

    async def timeout(*args, **kwargs):
        raise Conflict("repair_model_timeout")

    repairs.engine.repair = timeout
    asyncio.run(repairs.tick())
    exhausted = repairs.get(task["id"])
    assert exhausted["attempt"] == 4 and not exhausted["can_retry"]
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], exhausted["version"])
    assert not publisher.calls


def test_modified_original_after_review_never_reaches_publisher(pending):
    repairs, _, report, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    delegate = repairs.engine.repair

    async def mutate(context, event, *, workspace):
        result = await delegate(context, event, workspace=workspace)
        original = repairs.execution.directory(report["env_id"]) / "source/app.py"
        original.write_text("print('changed after review')\n")
        return result

    repairs.engine.repair = mutate
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["error"] == "repair_source_changed"
    assert not publisher.calls


def test_pausing_enabled_work_stops_model_tools_and_publication(pending):
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    delegate = repairs.engine.repair

    async def pause(context, event, *, workspace):
        repairs.configure(repairs.settings()["version"], False)
        return await delegate(context, event, workspace=workspace)

    repairs.engine.repair = pause
    asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["error"] == "repair_paused"
    assert not publisher.calls


def test_blocked_optional_file_returns_no_content_and_does_not_abort_unrelated_repair(pending):
    repairs, _, report, _, publisher = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    (source / "private_config.py").write_text('api_key = "never-expose-hardcoded-value"\n')
    task = confirm(pending)
    enable(repairs)

    class SkipBlockedModel(RepairModel):
        def __init__(self):
            super().__init__()
            self.offered = False
            self.inputs = []

        async def get_response(self, system_instructions, input, **kwargs):
            self.inputs.append(input)
            if not self.offered:
                self.offered = True
                return ModelResponse(
                    output=[ResponseFunctionToolCall(
                        id="blocked_0", call_id="blocked_0", type="function_call",
                        name="read_repair_source", arguments=json.dumps({
                            "path": "private_config.py", "candidate": False,
                        }),
                    )], usage=Usage(requests=1), response_id=None,
                )
            return await super().get_response(system_instructions, input, **kwargs)

    model = SkipBlockedModel()

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created" and len(publisher.calls) == 1
    events = [event for event in result["events"] if event["kind"] == "source_blocked"]
    assert len(events) == 1
    assert json.loads(events[0]["value"]) == {
        "path": "private_config.py", "phase": "read", "line": 1, "rule": "credential_literal",
    }
    assert "never-expose-hardcoded-value" not in json.dumps(model.inputs, default=str)
    assert "never-expose-hardcoded-value" not in json.dumps(result)
    assert any("repair_sensitive_content" in str(packet) for packet in model.inputs)


@pytest.mark.parametrize("reason", [
    "not_found", "candidate_not_ready", "too_large", "invalid_utf8", "read_error", "unsafe_path",
])
def test_unavailable_source_tool_can_recover_without_inventing_source(pending, monkeypatch, reason):
    repairs, _, report, _, publisher = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    deny_reads = [False]
    path = "app.py" if reason == "candidate_not_ready" else "optional.py"
    if reason == "unsafe_path":
        path = "../outside.py"
    if reason in ("too_large", "invalid_utf8", "read_error"):
        (source / path).write_bytes({
            "too_large": b"x" * 32001, "invalid_utf8": b"\xff", "read_error": b"safe optional file",
        }[reason])
    if reason == "read_error":
        original_open = Path.open

        def denied(target, *args, **kwargs):
            if deny_reads[0] and target.name == "optional.py" and target.parent.name == "original":
                raise PermissionError("private host path and provider data")
            return original_open(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    task = confirm(pending)
    enable(repairs)

    class CorrectingModel(RepairModel):
        def __init__(self):
            super().__init__()
            self.offered = False
            self.inputs = []

        async def get_response(self, system_instructions, input, **kwargs):
            self.inputs.append(input)
            if not self.offered:
                self.offered = True
                return ModelResponse(output=[ResponseFunctionToolCall(
                    id="unavailable_0", call_id="unavailable_0", type="function_call",
                    name="read_repair_source", arguments=json.dumps({
                        "path": path, "candidate": reason == "candidate_not_ready",
                    }),
                )], usage=Usage(requests=1), response_id=None)
            return await super().get_response(system_instructions, input, **kwargs)

    model = CorrectingModel()

    async def repair(context, event, *, workspace):
        deny_reads[0] = True
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    events = [e for e in result["events"] if e["kind"] == "source_unavailable"]
    if reason == "unsafe_path":
        assert result["error"] == "repair_unsafe_path" and not publisher.calls
        assert not events and model.calls == 0
        return
    assert result["status"] == "pr_created" and len(publisher.calls) == 1
    assert [json.loads(e["value"]) for e in events] == [{
        "path": path, "candidate": reason == "candidate_not_ready", "reason": reason,
    }]
    tool_output = ast.literal_eval(next(
        item["output"] for item in model.inputs[1]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ))
    assert tool_output["error"] == "repair_file_unavailable"
    assert "app.py" in tool_output["available_paths"]
    assert "sha256" not in tool_output and "text" not in tool_output
    assert "private host path" not in json.dumps(result)
    assert "private host path" not in json.dumps(model.inputs, default=str)


def test_unavailable_source_reads_consume_budget_and_cannot_authorize_edit(pending):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "5" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    for _ in range(24):
        with pytest.raises(SourceReadUnavailable) as caught:
            workspace.read("missing.py")
        assert caught.value.details["reason"] == "not_found"
    with pytest.raises(Conflict, match="repair_read_budget"):
        workspace.read("app.py")
    assert not workspace.reads and workspace.read_chars == 0
    with pytest.raises(Conflict, match="repair_source_changed"):
        workspace.prepare(Patch.model_validate(fixture_patch()))


@pytest.mark.parametrize("prior_timeout,new_policy,error,stale,allowed", [
    (False, False, "repair_file_unavailable", False, True),
    (True, False, "repair_file_unavailable", False, True),
    (True, True, "repair_file_unavailable", False, False),
    (True, False, "repair_tests_failed", False, False),
    (True, False, "repair_file_unavailable", True, False),
])
def test_source_read_upgrade_recovery_is_bounded_and_preserves_evidence_gates(
    pending, prior_timeout, new_policy, error, stale, allowed,
):
    from repowatch.repair_workspace import SOURCE_READ_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = dict(task["context"])
    if prior_timeout:
        context["timeout_recovery_used"] = True
    if new_policy:
        context["source_read_policy"] = SOURCE_READ_POLICY
    attempt = 3 + int(prior_timeout)
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=?,error=?,context=? WHERE id=?",
                   (attempt, error, json.dumps(context), task["id"]))
        if stale:
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is allowed
    assert not asyncio.run(repairs.tick())
    if not allowed:
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["attempt"] == attempt
    assert queued["attempts"][0]["error"] == error
    assert any(e["kind"] == "source_read_recovery" for e in queued["events"])

    async def unavailable(*args, **kwargs):
        raise SourceReadUnavailable("missing.py", False, "not_found")

    repairs.engine.repair = unavailable
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    assert final["attempt"] == attempt + 1 and not final["can_retry"]
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], final["version"])
    assert not publisher.calls


@pytest.mark.parametrize("invalid", ["json", "hash", "extra_field"])
def test_tool_arguments_are_corrected_before_execution_without_leaking_values(
    pending, caplog, invalid,
):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = RepairModel()
    delegate = model.get_response
    offered = False
    patch = fixture_patch()
    if invalid == "hash":
        patch["edits"][0]["original_sha256"] = "untrusted-private-value"
    if invalid == "extra_field":
        patch["untrusted-private-field"] = "untrusted-private-value"
    arguments = ('{"patch": untrusted-private-value' if invalid == "json" else
                 json.dumps({"patch": patch}))

    async def invalid_once(system_instructions, input, **kwargs):
        nonlocal offered
        if model.calls == 1 and not offered:
            offered = True
            return ModelResponse(output=[ResponseFunctionToolCall(
                id="malformed_0", call_id="malformed_0", type="function_call",
                name="submit_and_test_repair", arguments=arguments,
            )], usage=Usage(requests=1), response_id=None)
        return await delegate(system_instructions, input, **kwargs)

    model.get_response = invalid_once

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created" and len(publisher.calls) == 1
    errors = [json.loads(e["value"]) for e in result["events"]
              if e["kind"] == "tool_arguments_invalid"]
    assert len(errors) == 1 and errors[0]["tool"] == "submit_and_test_repair"
    assert errors[0]["reason"] == ("invalid_json" if invalid == "json" else "arguments_schema")
    assert errors[0]["corrections_remaining"] == 1
    if invalid == "hash":
        assert errors[0]["fields"] == ["patch.edits.[].original_sha256"]
    assert len(runtime.test_calls) == 7  # Only the corrected patch runs verification.
    assert "untrusted-private" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("shape", ["missing_wrapper", "encoded_patch"])
@pytest.mark.parametrize("invalid", ["hash", "extra_field", "missing_content"])
def test_normalized_arguments_still_require_the_canonical_tool_schema(shape, invalid):
    from agents.function_schema import function_schema
    from pydantic import ValidationError

    def submit(patch: Patch):
        return patch

    patch = fixture_patch()
    if invalid == "hash":
        patch["edits"][0]["original_sha256"] = "invalid-hash"
    elif invalid == "extra_field":
        patch["edits"][0]["unknown"] = "private-value"
    else:
        del patch["edits"][0]["content"]
    arguments, normalized = patch_arguments(json.dumps(
        patch if shape == "missing_wrapper" else {"patch": json.dumps(patch)},
    ))
    assert normalized == shape
    with pytest.raises(ValidationError):
        function_schema(submit).params_pydantic_model.model_validate_json(arguments)


@pytest.mark.parametrize("shape", ["missing_wrapper", "encoded_patch"])
@pytest.mark.parametrize("case", ["valid", "hash", "sensitive", "unsafe"])
def test_patch_transport_shapes_keep_schema_and_safety_gates(pending, shape, case):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = RepairModel()
    delegate = model.get_response

    async def shaped(*args, **kwargs):
        response = await delegate(*args, **kwargs)
        if model.calls == 2:
            patch = fixture_patch()
            if case == "hash":
                patch["edits"][0]["original_sha256"] = "f" * 64
            if case == "sensitive":
                patch["edits"][0]["content"] = 'api_key = "private-patch-credential"\n'
            if case == "unsafe":
                patch["edits"][0]["path"] = "../app.py"
            response.output[0].arguments = json.dumps(
                patch if shape == "missing_wrapper" else {"patch": json.dumps(patch)},
            )
        return response

    model.get_response = shaped

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    events = [json.loads(e["value"]) for e in result["events"]
              if e["kind"] == "tool_arguments_normalized"]
    assert events == [{"tool": "submit_and_test_repair", "shape": shape}]
    if case == "valid":
        assert result["status"] == "pr_created" and len(runtime.test_calls) == 7
        assert len(publisher.calls) == 1
    else:
        assert result["error"] == {
            "hash": "repair_source_changed", "sensitive": "repair_sensitive_content",
            "unsafe": "repair_unsafe_path",
        }[case]
        assert not runtime.test_calls and not publisher.calls
    assert "private-patch-credential" not in json.dumps(result)


@pytest.mark.parametrize("persistent", [False, True])
def test_neutral_new_regression_filename_can_be_corrected_without_weakening_paths(
    pending, persistent,
):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model, proposals = RepairModel(), []
    delegate = model.get_response

    async def named(system_instructions, input, **kwargs):
        if model.calls == 1 and (persistent or not proposals):
            proposals.append(True)
            patch = fixture_patch()
            patch["edits"][1]["path"] = patch["regression_paths"][0] = (
                "tests/test_secret_boundary.py"
            )
            return ModelResponse(output=[ResponseFunctionToolCall(
                id=f"path_{len(proposals)}", call_id=f"path_{len(proposals)}",
                type="function_call", name="submit_and_test_repair",
                arguments=json.dumps({"patch": patch}),
            )], usage=Usage(requests=1), response_id=None)
        return await delegate(system_instructions, input, **kwargs)

    model.get_response = named

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    rejected = [json.loads(e["value"]) for e in result["events"] if e["kind"] == "path_rejected"]
    assert rejected == [{"reason": "sensitive_name", "edit_index": 1}] * len(proposals)
    if persistent:
        assert len(proposals) == 3 and result["error"] == "repair_unsafe_path"
        assert not result["patch"] and not runtime.test_calls and not publisher.calls
    else:
        assert result["status"] == "pr_created" and len(runtime.test_calls) == 7
        assert len(publisher.calls) == 1


@pytest.mark.parametrize("invalid_first", [False, True])
def test_unverified_repair_cannot_offer_or_invoke_review_handoff(pending, invalid_first):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model, calls = RepairModel(), []

    async def premature(*args, **kwargs):
        calls.append(True)
        assert not kwargs["handoffs"]
        invalid = invalid_first and len(calls) <= 2
        return ModelResponse(output=[ResponseFunctionToolCall(
            id=f"call_{len(calls)}", call_id=f"call_{len(calls)}", type="function_call",
            name="submit_and_test_repair" if invalid else "transfer_to_independent_reviewer",
            arguments='{"patch":null}' if invalid else "{}",
        )], usage=Usage(requests=1), response_id=None)

    model.get_response = premature

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_verification_required"
    assert not result["verification"] and not result["patch"]
    assert not runtime.test_calls and not publisher.calls
    assert not any(e["kind"] == "handoff" for e in result["events"])


def test_repeated_invalid_tool_arguments_stop_with_specific_error_and_no_tests(pending, caplog):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = RepairModel()
    calls = []

    async def always_invalid(*args, **kwargs):
        calls.append(True)
        return ModelResponse(output=[ResponseFunctionToolCall(
            id=f"bad_{len(calls)}", call_id=f"bad_{len(calls)}", type="function_call",
            name="submit_and_test_repair", arguments='{"patch":"untrusted-private-value"}',
        )], usage=Usage(requests=1), response_id=None)

    model.get_response = always_invalid

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_tool_arguments_invalid" and len(calls) == 3
    assert not result["patch"] and not publisher.calls and not runtime.test_calls
    assert "untrusted-private-value" not in json.dumps(result) + caplog.text
    failures = [json.loads(e["value"]) for e in result["events"]
                if e["kind"] == "tool_arguments_invalid"]
    assert failures[0]["issues"] == ["object_required"]


def test_failure_classification_handles_sdk_wrappers_and_preserves_safety_gates():
    from agents.exceptions import ModelBehaviorError

    from repowatch.repository_source import ExecutionError

    invalid = ModelBehaviorError("Invalid JSON input for tool test: untrusted-private-value")
    assert repair_failure_code(invalid) == "repair_tool_arguments_invalid"
    wrapped = ModelBehaviorError("untrusted-private-value")
    wrapped.__cause__ = Conflict("repair_sensitive_content")
    assert repair_failure_code(wrapped) == "repair_sensitive_content"
    wrapped.__cause__ = ExecutionError("docker_unavailable")
    assert repair_failure_code(wrapped) == "docker_unavailable"
    unexpected = RuntimeError("untrusted-private-value")
    unexpected.__context__ = PermissionError("private-host-path")
    assert repair_failure_code(unexpected) == "repair_workspace_io_error"
    assert repair_failure_code(ModelBehaviorError("untrusted-private-value")) == (
        "repair_output_invalid"
    )
    assert repair_failure_code(ValueError("untrusted-private-value")) == "repair_failed"
    details = repair_failure_details(wrapped)
    assert details["exceptions"] == ["ExecutionError", "ModelBehaviorError"]
    assert "untrusted-private-value" not in json.dumps(details)


@pytest.mark.parametrize("status,code", [
    (400, "repair_provider_request_rejected"), (401, "repair_authentication_failed"),
    (403, "repair_provider_forbidden"), (404, "repair_provider_not_found"),
    (422, "repair_provider_request_rejected"), (429, "repair_rate_limited"),
    (503, "repair_provider_error"),
])
def test_provider_failures_persist_only_safe_actionable_codes(pending, caplog, status, code):
    import httpx
    from openai import APIStatusError, AuthenticationError, RateLimitError

    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.example"))
    error_type = {401: AuthenticationError, 429: RateLimitError}.get(status, APIStatusError)

    async def denied(*args, **kwargs):
        raise error_type("untrusted-private-error", response=response,
                         body={"key": "untrusted-private-body"})

    repairs.engine.repair = denied
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == code
    diagnostics = [json.loads(e["value"]) for e in result["events"]
                   if e["kind"] == "failure_details"]
    assert len(diagnostics) == 1 and diagnostics[0]["code"] == code
    assert diagnostics[0]["locations"]
    assert all(p["file"].startswith("repowatch/") and p["line"] > 0
               for p in diagnostics[0]["locations"])
    assert "untrusted-private" not in json.dumps(result) + caplog.text
    assert not publisher.calls and not runtime.test_calls


@pytest.mark.parametrize("new_policy,artifacts,stale,allowed", [
    (False, False, False, True), (True, False, False, False),
    (False, True, False, False), (False, False, True, False),
])
def test_legacy_generic_failure_recovery_is_explicit_and_cannot_replay_artifacts(
    pending, new_policy, artifacts, stale, allowed,
):
    from repowatch.repair_model import FAILURE_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = {**task["context"], "timeout_recovery_used": True, "source_read_recovery_used": True}
    if new_policy:
        context["failure_policy"] = FAILURE_POLICY
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=5,error='repair_failed',"
                   "context=?,patch=? WHERE id=?",
                   (json.dumps(context), json.dumps(fixture_patch()) if artifacts else None,
                    task["id"]))
        if stale:
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is allowed
    assert not asyncio.run(repairs.tick())
    if not allowed:
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["attempt"] == 5 and queued["attempts"][0]["error"] == "repair_failed"
    assert any(e["kind"] == "failure_recovery" for e in queued["events"])

    async def unknown(*args, **kwargs):
        raise ValueError("untrusted-private-value")

    repairs.engine.repair = unknown
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    assert final["attempt"] == 6 and not final["can_retry"]
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], final["version"])
    assert "untrusted-private-value" not in json.dumps(final)
    assert not publisher.calls


@pytest.mark.parametrize("case", [
    "eligible", "new_policy", "used", "patch", "verification", "remote", "stale",
    "invalid_json", "old_attempt", "only_one", "latest_complete", "invalid_metadata",
])
@pytest.mark.parametrize("failure_code", ["repair_output_invalid", "repair_output_truncated"])
def test_legacy_truncation_recovery_requires_latest_evidence_and_is_bounded(
    pending, case, failure_code,
):
    from repowatch.repair_model import OUTPUT_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = {**task["context"], "timeout_recovery_used": True,
               "source_read_recovery_used": True, "failure_recovery_used": True,
               "output_policy": "bounded_reasoning_output_v1"}
    if case == "new_policy":
        context["output_policy"] = OUTPUT_POLICY
    if case == "used":
        context["output_recovery_used"] = True
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=?,error=?,context=?,"
                   "patch=?,verification=?,remote=? WHERE id=?",
                   (7 if case == "used" else 6,
                    "repair_failed" if case == "invalid_json" else failure_code,
                    json.dumps(context), "{}" if case == "patch" else None,
                    "{}" if case == "verification" else None,
                    "{}" if case == "remote" else None, task["id"]))
        if case == "stale":
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    repairs.event(task["id"], "retry", "6")
    repairs.event(task["id"], "provider_response", json.dumps({"truncated": True}))
    if case != "only_one":
        repairs.event(task["id"], "provider_response", json.dumps({"truncated": True}))
    if case == "old_attempt":
        repairs.event(task["id"], "retry", "6")
    if case == "latest_complete":
        repairs.event(task["id"], "provider_response", json.dumps({"truncated": False}))
    if case == "invalid_metadata":
        repairs.event(task["id"], "provider_response", "[]")
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is (case == "eligible")
    assert not asyncio.run(repairs.tick())
    if case != "eligible":
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["attempt"] == 6 and queued["context"]["output_recovery_used"] is True
    assert queued["attempts"][0]["error"] == failure_code
    assert any(e["kind"] == "output_recovery" for e in queued["events"])

    async def truncated(*args, **kwargs):
        raise Conflict("repair_output_truncated")

    repairs.engine.repair = truncated
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    assert final["attempt"] == 7 and not final["can_retry"]
    assert final["context"]["output_policy"] == OUTPUT_POLICY
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], final["version"])
    assert not publisher.calls


@pytest.mark.parametrize("case", [
    "eligible", "new_policy", "used", "patch", "verification", "remote", "stale",
    "only_one", "old_attempt", "tested", "invalid_metadata", "wrong_field", "wrong_error",
])
def test_legacy_premature_handoff_recovery_requires_unexecuted_patch_evidence(pending, case):
    from repowatch.repair_model import PROTOCOL_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = {**task["context"], "timeout_recovery_used": True,
               "source_read_recovery_used": True, "failure_recovery_used": True,
               "output_recovery_used": True}
    if case == "new_policy":
        context["protocol_policy"] = PROTOCOL_POLICY
    if case == "used":
        context["protocol_recovery_used"] = True
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',attempt=?,error=?,context=?,"
                   "patch=?,verification=?,remote=? WHERE id=?",
                   (8 if case == "used" else 7,
                    "repair_failed" if case == "wrong_error" else "repair_tests_failed",
                    json.dumps(context), "{}" if case == "patch" else None,
                    "{}" if case == "verification" else None,
                    "{}" if case == "remote" else None, task["id"]))
        if case == "stale":
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    repairs.event(task["id"], "retry", "7")
    for _ in range(1 if case == "only_one" else 2):
        repairs.event(task["id"], "tool_arguments_invalid", json.dumps({
            "tool": "submit_and_test_repair", "reason": "arguments_schema",
            "fields": ["path"] if case == "wrong_field" else ["patch"],
        }))
    if case == "old_attempt":
        repairs.event(task["id"], "retry", "7")
    if case == "tested":
        repairs.event(task["id"], "testing", "regression_before")
    if case == "invalid_metadata":
        repairs.event(task["id"], "tool_arguments_invalid", "not-json")
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is (case == "eligible")
    assert not asyncio.run(repairs.tick())
    if case != "eligible":
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["attempt"] == 7 and queued["context"]["protocol_recovery_used"] is True
    assert queued["attempts"][0]["error"] == "repair_tests_failed"
    assert any(e["kind"] == "protocol_recovery" for e in queued["events"])

    async def incomplete(*args, **kwargs):
        raise Conflict("repair_verification_required")

    repairs.engine.repair = incomplete
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    assert final["attempt"] == 8 and not final["can_retry"]
    assert final["context"]["protocol_policy"] == PROTOCOL_POLICY
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], final["version"])
    assert not publisher.calls


@pytest.mark.parametrize("kind", ["docker", "path"])
@pytest.mark.parametrize("case", [
    "eligible", "new_policy", "used", "stale", "remote", "verification", "review",
    "missing_proof", "old_proof", "wrong_error",
])
def test_workspace_recovery_is_explicit_bounded_and_artifact_gated(pending, kind, case):
    from repowatch.repairs import WORKSPACE_POLICY

    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    context = {**task["context"]}
    if case == "new_policy":
        context["workspace_policy"] = WORKSPACE_POLICY
    if case == "used":
        context["workspace_recovery_used"] = True
    error = "docker_unavailable" if kind == "docker" else "repair_unsafe_path"
    with repairs.store.connection() as db:
        account = db.execute("SELECT a.id,a.version FROM github_accounts a "
                             "JOIN github_configuration c ON c.active_account_id=a.id").fetchone()
        context.update(account_id=account["id"], account_version=account["version"])
        db.execute("UPDATE repair_tasks SET status='failed',attempt=?,error=?,context=?,"
                   "patch=?,source_digest=?,verification=?,review=?,remote=? WHERE id=?",
                   (4 if case == "used" else 3,
                    "repair_review_rejected" if case == "wrong_error" else error,
                    json.dumps(context),
                    json.dumps(fixture_patch()) if kind == "docker" and case != "missing_proof"
                    else None,
                    "a" * 64 if case != "old_proof" else None,
                    '{"regression_before":{"exit_code":1}}' if case == "verification" else None,
                    '{"approved":false}' if case == "review" else None,
                    "{}" if case == "remote" else None, task["id"]))
        if case == "stale":
            db.execute("UPDATE environments SET revision='stale' WHERE id=?", (task["env_id"],))
    repairs.event(task["id"], "retry", "3")
    if case != "missing_proof":
        repairs.event(task["id"], "tool_failure", json.dumps({
            "tool": "submit_and_test_repair", "code": error,
        }))
    if case == "old_proof" and kind == "path":
        repairs.event(task["id"], "retry", "3")
    failed = repairs.get(task["id"])
    assert failed["can_retry"] is (case == "eligible")
    assert not asyncio.run(repairs.tick())
    if case != "eligible":
        with pytest.raises(Conflict, match="repair_changed|repair_stale"):
            repairs.retry(task["id"], failed["version"])
        assert not publisher.calls
        return
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"] - 1)
    queued = repairs.retry(task["id"], failed["version"])
    assert queued["attempt"] == 3 and queued["context"]["workspace_recovery_used"] is True
    assert queued["attempts"][0]["error"] == error
    assert any(e["kind"] == "workspace_recovery" for e in queued["events"])

    async def rejected(*args, **kwargs):
        raise Conflict("repair_unsafe_path")

    repairs.engine.repair = rejected
    asyncio.run(repairs.tick())
    final = repairs.get(task["id"])
    assert final["attempt"] == 4 and not final["can_retry"]
    assert final["context"]["workspace_policy"] == WORKSPACE_POLICY
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], final["version"])
    assert not publisher.calls


def test_sensitive_patch_fails_closed_with_safe_diagnostics_before_tests_or_publication(pending):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    model = RepairModel()
    delegate = model.get_response

    async def unsafe(*args, **kwargs):
        result = await delegate(*args, **kwargs)
        if model.calls == 2:
            patch = fixture_patch()
            patch["edits"][0]["content"] = 'api_key: str = "never-expose-hardcoded-value"\n'
            result.output[0].arguments = json.dumps({"patch": patch})
        return result

    model.get_response = unsafe

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["error"] == "repair_sensitive_content"
    blocked = next(event for event in result["events"] if event["kind"] == "source_blocked")
    assert json.loads(blocked["value"])["phase"] == "patch"
    assert not publisher.calls and not runtime.test_calls
    assert result["patch"] is None
    assert "never-expose-hardcoded-value" not in json.dumps(result)


def test_python_declarations_read_exact_source_and_patch_without_log_redaction(pending):
    repairs, _, report, _, _ = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    text = 'def make_client(api_key: str):\n    return Client(api_key=api_key)\n'
    (source / "app.py").write_bytes(text.encode())
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "1" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    observed = workspace.read("app.py")
    assert observed["text"] == text and observed["sha256"] == digest(text.encode())
    patch = fixture_patch()
    patch["edits"][0].update(original_sha256=observed["sha256"], content=text + "# narrow fix\n")
    workspace.prepare(Patch.model_validate(patch))
    assert "api_key: str" in (workspace.candidate / "app.py").read_text()


def test_denied_source_reads_still_consume_bounded_read_budget(pending):
    repairs, _, report, _, _ = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    (source / "app.py").write_text('api_key="never-expose-hardcoded-value"\n')
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "2" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    for _ in range(24):
        with pytest.raises(SourceContentBlocked) as caught:
            workspace.read("app.py")
        assert caught.value.details["line"] == 1
        assert "never-expose-hardcoded-value" not in str(caught.value.details)
    with pytest.raises(Conflict, match="repair_read_budget"):
        workspace.read("app.py")
    assert "app.py" not in workspace.reads


@pytest.mark.parametrize("change", ["config", "execution", "revision", "deleted"])
def test_stale_confirmation_cannot_run_or_retry(pending, change):
    repairs, _, report, runtime, publisher = pending
    task = confirm(pending)
    env = repairs.execution.get(report["env_id"])
    if change == "config":
        repairs.execution.configure(env["id"], env["version"], {**env["config"], "test": "changed"})
    elif change == "execution":
        repairs.execution.action(env["id"], env["version"], "test")
        repairs.execution.tick()
    elif change == "revision":
        repairs.execution.update(env["id"], revision="f" * 40)
    else:
        repairs.execution.update(env["id"], state="deleted")
    enable(repairs)
    assert not asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["attempt"] == 0
    assert result["error"] == "repair_stale"
    with pytest.raises(Conflict, match="repair_stale"):
        repairs.retry(task["id"], result["version"])
    assert not repairs.engine.models and not publisher.calls


def test_publication_retry_reuses_verified_patch_without_replaying_model_or_tests(pending):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    publisher.fail = True
    assert asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    assert failed["error"] == "github_unreachable" and failed["review"]["approved"]
    tests_before = len(runtime.test_calls)
    repairs.retry(task["id"], failed["version"])
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"])
    publisher.fail = False
    assert asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["status"] == "pr_created"
    assert len(repairs.engine.models) == 1 and len(runtime.test_calls) == tests_before


def test_settings_and_attempts_are_versioned_and_disabled_by_default(pending):
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    assert not repairs.settings()["enabled"]
    enable(repairs)
    with pytest.raises(Conflict, match="repair_settings_changed"):
        repairs.configure(1, False)
    runtime.regression_after = 1
    for attempt in range(1, 4):
        assert asyncio.run(repairs.tick())
        failed = repairs.get(task["id"])
        assert failed["attempt"] == attempt
        if attempt < 3:
            repairs.retry(task["id"], failed["version"])
    assert failed["error"] == "repair_tests_failed"
    assert not publisher.calls and len(repairs.engine.models) == 3
    assert not failed["publication_retry_available"]
    assert not failed["can_retry"]
    with pytest.raises(Conflict, match="repair_changed"):
        repairs.retry(task["id"], failed["version"])


@pytest.mark.parametrize("path", [
    "../app.py", "/app.py", "a\\app.py", "C:/app.py", ".git/config.py", ".github/ci.py",
    "node_modules/app.py", "tests/../app.py", "data/x.py", "app.py.", "con.py", "api_token.py",
    "setup.py", "tests/conftest.py",
])
def test_repair_paths_cannot_escape_or_modify_protected_locations(tmp_path, path):
    with pytest.raises(Conflict, match="repair_unsafe_path"):
        safe_path(tmp_path, path)


@pytest.mark.parametrize("change,error", [
    ("unread", "repair_source_changed"), ("hash", "repair_source_changed"),
    ("existing_test", "repair_existing_tests_protected"),
    ("duplicate", "repair_unsafe_path"), ("missing_regression", "repair_regression_required"),
    ("existing_regression", "repair_regression_required"),
])
def test_patch_validation_is_fail_closed_and_leaves_original_untouched(pending, change, error):
    repairs, _, report, _, _ = pending
    env = repairs.execution.get(report["env_id"])
    workspace = RepairWorkspace(repairs.execution, "repair_" + "a" * 16, env, lambda: None)
    workspace.read("app.py")
    patch = fixture_patch()
    if change == "unread":
        workspace.reads.clear()
    elif change == "hash":
        patch["edits"][0]["original_sha256"] = "0" * 64
    elif change == "existing_test":
        existing = workspace.original / "tests/test_original.py"
        existing.parent.mkdir()
        existing.write_text("assert True\n")
        patch["edits"][0]["path"] = "tests/test_original.py"
    elif change == "duplicate":
        patch["edits"].append(copy.deepcopy(patch["edits"][0]))
    elif change == "missing_regression":
        patch["regression_paths"] = ["tests/missing.py"]
    else:
        existing = workspace.original / "tests/test_repowatch.py"
        existing.parent.mkdir()
        existing.write_text("assert True\n")
    with pytest.raises(Conflict, match=error):
        workspace.prepare(Patch.model_validate(patch))
    assert not workspace.candidate.exists()
    assert (workspace.original / "app.py").read_text() == "print('ok')"


@pytest.mark.parametrize("name", ["testbounds.py", "bounds.test.tsx", "bounds.spec.jsx"])
def test_existing_test_names_are_protected_outside_tests_directory(name):
    assert is_test(name)


def test_new_regression_cannot_change_package_initialization(pending):
    repairs, _, report, _, _ = pending
    workspace = RepairWorkspace(
        repairs.execution, "repair_" + "d" * 16, repairs.execution.get(report["env_id"]),
        lambda: None,
    )
    workspace.read("app.py")
    patch = fixture_patch()
    patch["edits"][1]["path"] = patch["regression_paths"][0] = "tests/__init__.py"
    with pytest.raises(Conflict, match="repair_regression_required"):
        workspace.prepare(Patch.model_validate(patch))


def test_api_requires_owner_csrf_and_explicit_feedback_before_queueing(tmp_path):
    app = create_app(
        tmp_path / "api.db", start_worker=False, github_client=Remote().client,
        credential_vault=MemoryVault(), execution_runtime=RepairRuntime(),
        repository_source=Source(), analysis_engine=Engine(), repair_engine=RepairEngine(),
        repair_publisher=Publisher(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/repair-settings").status_code == 401
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        assert client.post(
            "/api/repair-settings", json={"version": 1, "enabled": True}
        ).status_code == 403
        assert client.post("/api/repair-settings", headers=headers, json={
            "version": 1, "enabled": True, "merge_enabled": True,
        }).status_code == 422
        execution = app.state.execution
        execution.github.connect("alice", TOKEN_A)
        repo = execution.store.add_repository("alice/project", "https://github.com/alice/project")
        env = prepare_tested((execution, repo, execution.runtime))
        asyncio.run(app.state.analyses.tick())
        report = app.state.analyses.list(env["id"])[0]
        url = f"/api/analyses/{report['id']}/findings/{report['findings'][0]['id']}/feedback"
        assert client.post(url, headers=headers, json={
            "version": 1, "decision": "confirmed",
        }).status_code == 409
        assert app.state.repairs.list(env["id"]) == []
        assert client.post(url, headers=headers, json={
            "version": 1, "decision": "confirmed", "personally_reproduced": True,
        }).status_code == 200
        tasks = client.get(f"/api/environments/{env['id']}/repairs").json()
        assert len(tasks) == 1 and tasks[0]["status"] == "queued"
        assert not tasks[0]["enabled"]
        notice = client.get("/api/analysis-notifications").json()[0]
        assert notice["repair_queued"] == 1 and not notice["repairs_enabled"]
        assert TOKEN_A not in json.dumps(tasks)
        assert client.post("/api/repair-settings", headers=headers, json={
            "version": 1, "enabled": True,
        }).status_code == 200
        assert asyncio.run(app.state.repairs.tick())
        result = client.get("/api/repairs/" + tasks[0]["id"]).json()
        assert result["status"] == "pr_created"
        notice = client.get("/api/analysis-notifications").json()[0]
        assert notice["repair_published"] == 1 and notice["repair_queued"] == 0
        assert client.post("/api/repairs/" + tasks[0]["id"] + "/merge", headers=headers,
                           json={}).status_code in (404, 405)


@pytest.mark.parametrize("role", ["repair_agent", "independent_reviewer"])
@pytest.mark.parametrize("effort", ["medium", "high", "xhigh"])
def test_output_recovery_reduces_reasoning_and_keeps_it_for_later_calls(role, effort):
    settings = ModelSettings(max_tokens=12000, reasoning=Reasoning(effort=effort, summary="auto"),
                             parallel_tool_calls=False, extra_body={"owner_option": True})
    requests, events = [], []

    async def provider(**kwargs):
        current = kwargs["model_settings"]
        requests.append(current)
        # Merely increasing the token limit still exhausts reasoning, as in the live failure.
        if current.reasoning.effort != "low":
            raise OutputTruncated("private-unfinished-tool-arguments")
        return SimpleNamespace(output=[])

    workspace = SimpleNamespace(check=lambda: None, verification=None)
    model = RepairStageModel(SimpleNamespace(get_response=provider), role, workspace,
                             lambda *args: events.append(args), dict, {"timeouts": 0})
    for _ in range(2):
        asyncio.run(model.get_response("trusted instructions", [], handoffs=[], tools=[],
                                      model_settings=settings))
    assert [s.reasoning.effort for s in requests] == [effort, "low", "low"]
    assert [s.max_tokens for s in requests] == [12000, 16000, 12000]
    assert all(s.reasoning.summary == "auto" and s.extra_body == {"owner_option": True}
               and s.parallel_tool_calls is False for s in requests)
    assert settings.reasoning.effort == effort and settings.max_tokens == 12000
    reduced = [json.loads(value) for kind, value in events if kind == "output_reasoning_reduced"]
    assert reduced == [{"role": role, "from": effort, "to": "low"}]
    assert "private-unfinished-tool-arguments" not in json.dumps(events)


@pytest.mark.parametrize("effort", [None, "low", "minimal", "none"])
def test_output_recovery_preserves_default_and_already_low_reasoning(effort):
    settings = ModelSettings(max_tokens=12000,
                             reasoning=Reasoning(effort=effort) if effort else None)
    requests, events = [], []

    async def provider(**kwargs):
        requests.append(kwargs["model_settings"])
        if len(requests) == 1:
            raise OutputTruncated("private-provider-body")
        return SimpleNamespace(output=[])

    model = RepairStageModel(SimpleNamespace(get_response=provider), "repair_agent",
                             SimpleNamespace(check=lambda: None, verification=None),
                             lambda *args: events.append(args), dict, {"timeouts": 0})
    asyncio.run(model.get_response("", [], handoffs=[], tools=[], model_settings=settings))
    assert [s.max_tokens for s in requests] == [12000, 16000]
    assert all(s.reasoning == settings.reasoning for s in requests)
    assert not any(kind == "output_reasoning_reduced" for kind, _ in events)
