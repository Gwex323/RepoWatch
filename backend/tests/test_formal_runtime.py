import json

import httpx
import pytest
from langchain_core.tools import tool
from test_roles import bind, diagnose
from test_roles import kernel as kernel

from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.runtime_stages import RuntimeStages
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime, StagePlan, ToolBinding


def start(k, *, policy=None):
    tasks = TaskRecords(k.store)
    repo = tasks.register_repository("runtime-fixture", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, "runtime-fixture", now=0)
    with k.store.write() as records:
        records.change("repositories", {"id": repo}, {"target_sha": "a" * 40})
        records.change(
            "tasks",
            {"id": task},
            {
                "configuration_json": canonical_json(
                    {
                        "code_version": {
                            "base_sha": "a" * 40,
                            "tree_sha": "a" * 40,
                            "content_manifest_sha256": "b" * 64,
                        },
                        "runtime_policy": policy or {},
                    }
                )
            },
        )
    k.budgets.authorize_initial(
        "runtime-budget",
        task,
        BudgetAmounts(
            active_ms=1000000, model_calls=30, tokens=1000000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="受控运行夹具",
        now=0,
    )
    k.queue.set_pause(False, now=0)
    return task, k.queue.claim(now=0)


def fixture_wire(*, child=False, wrong=False, forbidden=False, status="completed"):
    calls = []

    def wire(request):
        body = json.loads(request.content)
        calls.append(body)
        envelope = next(
            json.loads(item["content"])
            for item in body["messages"]
            if item["role"] == "user"
            and isinstance(item.get("content"), str)
            and item["content"].startswith('{"protocol_version"')
        )
        tool_results = [item for item in body["messages"] if item["role"] == "tool"]
        if not tool_results:
            if child and body["model"] == "model":
                name, args = (
                    "task",
                    {"description": "Run the bound read task", "subagent_type": "child-read"},
                )
            else:
                name, args = (
                    ("write_file", {"file_path": "outside", "content": "forbidden"})
                    if forbidden
                    else ("read_source", {"path": "fixture.py"})
                )
        else:
            name = "StructuredResult"
            context = {**envelope["context"]}
            if wrong:
                context["run_id"] = "forged-run"
            args = {
                "context": context,
                "mode": envelope["mode"],
                "status": status,
                "summary": "controlled fixture result",
                "artifacts": [],
                "unresolved": [],
            }
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": str(len(calls)),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    return wire, calls


def plan(reads, *, children=()):
    @tool(description="Read only the controlled fixture source")
    def read_source(path: str) -> str:
        if path != "fixture.py":
            raise PermissionError("fixture path rejected")
        reads.append(path)
        return "def add(a,b): return a+b"

    def verify(result, events):
        assert reads and result.status == "completed"
        return {
            "comparison": {"action": "read_fixture", "semantic_state": "checked"},
            "verified_fact_ids": [],
        }

    return StagePlan(
        "main",
        "primary",
        "Read the fixture and return StructuredResult with the exact context.",
        "investigate",
        "processor",
        (ToolBinding(read_source, BudgetAmounts(active_ms=1000)),),
        verify,
        children,
    )


def services(k, wire):
    k.clock = lambda: 0
    k.adapter = ProviderAdapter(k.providers, transport=httpx.MockTransport(wire))
    return k


def test_actual_sdk_deep_agents_checkpoint_and_first_real_reservation(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    task, token = start(k)
    wire, calls = fixture_wire()
    runtime = FormalRuntime(services(k, wire), wait=lambda _: None)
    reads = []
    stage = plan(reads)
    result = runtime.run(token, stage)
    assert result.context.run_id == token.run_id and reads == ["fixture.py"]
    assert len(calls) == 2
    assert k.budgets.view(task)["used"]["model_calls"] == 2
    assert k.budgets.view(task)["used"]["tokens"] == 36
    assert runtime.run(token, stage) == result
    assert len(calls) == 2
    with k.store.read() as records:
        intent = records.find("stage_intents", run_id=token.run_id)[0]
        assert intent["state"] == "checkpoint_saved"
        assert intent["checkpoint_id"]
        reference = records.one(
            "budget_reservations",
            id=json.loads(calls[0]["messages"][-1]["content"])["budget"]["reservation_id"],
        )
        assert reference["state"] == "settled"


@pytest.mark.parametrize("wrong,forbidden", [(True, False), (False, True)])
def test_wrong_context_and_unregistered_builtin_cannot_complete(kernel, wrong, forbidden):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k)
    wire, calls = fixture_wire(wrong=wrong, forbidden=forbidden)
    reads = []
    with pytest.raises((ValueError, PermissionError)):
        FormalRuntime(services(k, wire), wait=lambda _: None).run(token, plan(reads))
    assert reads == ([] if forbidden else ["fixture.py"])
    with k.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert records.find("stage_intents", run_id=token.run_id)[0]["state"] == "intent"


def test_serial_child_has_independent_run_model_and_shared_ledger(kernel):
    k = kernel
    bind(k)
    bind(k, "subtask.investigator", model="child-model")
    diagnose(k, "processor")
    diagnose(k, "processor", key="child-model-capability", model="child-model")
    task, token = start(k)
    wire, calls = fixture_wire(child=True)
    reads = []
    parent = plan(reads)
    child = StagePlan(
        "child-read",
        "primary",
        "Read the fixture in an independent bounded session.",
        "investigate",
        "subtask.investigator",
        parent.tools,
        parent.verifier,
    )
    parent = plan(reads, children=(child,))
    result = FormalRuntime(services(k, wire), wait=lambda _: None).run(token, parent)
    assert result.context.run_id == token.run_id
    assert [item["model"] for item in calls] == ["model", "child-model", "child-model", "model"]
    assert reads == ["fixture.py"]
    assert k.budgets.view(task)["used"]["model_calls"] == 4
    with k.store.read() as records:
        children = records.find("runs", parent_run_id=token.run_id)
        assert len(children) == 1 and children[0]["state"] == "ended"
        assert children[0]["budget_owner_task_id"] == task
        assert records.one("runs", id=token.run_id)["active_child_run_id"] is None


def test_step_safety_is_persistent_and_rejects_next_tool_before_execution(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k, policy={"max_steps": 1})
    wire, calls = fixture_wire()
    reads = []
    with pytest.raises(PermissionError):
        FormalRuntime(services(k, wire), wait=lambda _: None).run(token, plan(reads))
    assert reads == [] and len(calls) == 1
    with k.store.read() as records:
        assert records.find("runtime_stages", run_id=token.run_id)[0]["steps"] == 1
        assert records.find("block_reasons", code="stage_step_limit")


def test_checkpoint_write_failure_after_tool_stops_next_model_and_never_confirms_result(
    kernel, monkeypatch
):
    import sqlite3

    from repowatch.adapters.storage.checkpoints import PreparedSqliteSaver
    from repowatch.application.storage import StorageError

    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k)
    wire, calls = fixture_wire()
    reads = []
    original = PreparedSqliteSaver.put

    def failed(saver, *args, **kwargs):
        if reads:
            raise sqlite3.OperationalError("injected checkpoint write failure")
        return original(saver, *args, **kwargs)

    monkeypatch.setattr(PreparedSqliteSaver, "put", failed)
    with pytest.raises(StorageError):
        FormalRuntime(services(k, wire), wait=lambda _: None).run(token, plan(reads))
    with pytest.raises(StorageError):
        k.queue.claim(now=0)
    with k.store.read() as records:
        assert records.find("stage_intents", run_id=token.run_id)[0]["state"] == "intent"


def test_stage_effective_time_limit_blocks_before_new_sdk_work(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k, policy={"max_active_ms": 10})
    stages = RuntimeStages(k.store, k.artifacts)
    stage = stages.begin(token, "time-limit", "primary", now=0)
    with pytest.raises(PermissionError):
        stages.check(token, stage["id"], now=1)
    with k.store.read() as records:
        assert records.find("block_reasons", code="stage_timeout")


def test_confirmed_stage_missing_checkpoint_cannot_be_reused_or_issue_new_request(kernel):
    from repowatch.adapters.storage.checkpoints import checkpoint_store
    from repowatch.application.storage import StorageError

    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k)
    wire, calls = fixture_wire()
    runtime = FormalRuntime(services(k, wire), wait=lambda _: None)
    stage = plan([])
    runtime.run(token, stage)
    with k.store.read() as records:
        intent = records.find("stage_intents", run_id=token.run_id)[0]
    with checkpoint_store(k.settings) as saver:
        saver.delete_thread(intent["thread_id"])
    with pytest.raises(StorageError):
        runtime.run(token, stage)
    assert len(calls) == 2
    with pytest.raises(StorageError):
        k.queue.claim(now=0)


def test_model_reported_block_is_preserved_and_does_not_become_task_success(kernel):
    from dataclasses import replace

    k = kernel
    bind(k)
    diagnose(k, "processor")
    _, token = start(k)
    wire, calls = fixture_wire(status="blocked")
    stage = replace(
        plan([]), verifier=lambda result, events: {"comparison": {"obstacle": "fixture"}}
    )
    result = FormalRuntime(services(k, wire), wait=lambda _: None).run(token, stage)
    assert result.status == "blocked" and len(calls) == 2
    with k.store.read() as records:
        assert records.find("block_reasons", code="runtime_reported_blocked")
        assert records.one("global_control", singleton=1)["paused"] == 1
