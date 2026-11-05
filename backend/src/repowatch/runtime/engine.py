"""正式 LangGraph／Deep Agents 引擎，预算、权限、阶段和子运行均由宿主约束。"""

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool

from repowatch.adapters.providers import ModelLimits, ProviderRequestError
from repowatch.adapters.storage.checkpoints import checkpoint_store
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.delegation import Delegation
from repowatch.application.progress import ProgressService
from repowatch.application.provider_calls import ModelCalls, RetryPolicy
from repowatch.application.runtime_stages import RuntimeStages
from repowatch.application.storage import StorageError
from repowatch.application.task_records import block, execution_record, required
from repowatch.domain.budget import BudgetAmounts, BudgetDenied
from repowatch.domain.protocols import (
    BudgetReference,
    CodeVersion,
    RunContext,
    StructuredResult,
    TaskEnvelope,
    ToolPolicy,
    validate_result,
)
from repowatch.domain.task_records import canonical_json, stable_id
from repowatch.runtime.prototype import MissingStructuredResult
from repowatch.security import DEFAULT_REDACTOR
from repowatch.worker.model_calls import managed_role_model


@dataclass(frozen=True)
class ToolBinding:
    tool: BaseTool
    upper: BudgetAmounts


@dataclass(frozen=True)
class StagePlan:
    key: str
    objective_key: str
    objective: str
    mode: str
    role_key: str
    tools: tuple[ToolBinding, ...]
    verifier: Callable
    children: tuple["StagePlan", ...] = ()
    code_version: dict | None = None


class SerialGate:
    def __init__(self):
        self.lock = threading.Lock()

    @contextmanager
    def enter(self):
        if not self.lock.acquire(blocking=False):
            raise PermissionError("父子模型／工具不能并行")
        try:
            yield
        finally:
            self.lock.release()


class RuntimeBoundary(AgentMiddleware):
    def __init__(self, envelope, token, scope, gate, executor, bindings, child_names, events):
        self.token = token
        self.envelope, self.scope, self.gate, self.executor = envelope, scope, gate, executor
        self.bindings, self.child_names, self.events = bindings, child_names, events
        self.tool_index = 0

    def wrap_model_call(self, request, handler):
        names = set(self.envelope.tool_policy.allowed) | ({"task"} if self.child_names else set())
        tools = [item for item in request.tools if getattr(item, "name", None) in names]
        self.scope.preflight(self.token)
        with self.gate.enter():
            return handler(request.override(tools=tools))

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        self.scope.preflight(self.token)
        if name == "task":
            if request.tool_call["args"].get("subagent_type") not in self.child_names:
                raise PermissionError("只能委派预先绑定的同步子任务")
            self.scope.touch(self.token)
            return handler(request)
        self.envelope.tool_policy.require(name)
        binding = self.bindings[name]
        self.tool_index += 1

        def execute():
            self.scope.touch(self.token)
            with self.gate.enter():
                result = handler(request)
                self.events.append(
                    {
                        "tool": name,
                        "arguments": DEFAULT_REDACTOR.clean(request.tool_call["args"]),
                        "result": DEFAULT_REDACTOR.clean(getattr(result, "content", None)),
                    }
                )
                return result

        return self.executor.execute(
            self.token,
            f"tool:{self.scope.stage_id}:{self.tool_index}",
            binding.upper,
            execute,
            lambda _: {},
            purpose="tool",
        )


class FormalRuntime:
    def __init__(self, services, *, wait=time.sleep):
        self.services, self.wait = services, wait
        self.stages = RuntimeStages(services.store, services.artifacts)
        self.progress = ProgressService(services.store, services.artifacts, services.budgets)
        self.delegation = Delegation(services.store)
        self.lock = threading.Lock()

    def run(self, token, plan: StagePlan, *, system_prompt=None):
        if not self.lock.acquire(blocking=False):
            raise PermissionError("正式引擎已有执行者")
        try:
            with checkpoint_store(self.services.settings) as saver:
                return self._run(token, plan, saver, SerialGate(), system_prompt=system_prompt)
        finally:
            self.lock.release()

    def _run(self, token, plan, saver, gate, *, system_prompt=None):
        services = self.services
        stage = self.stages.begin(token, plan.key, plan.objective_key, now=services.clock())
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            snapshot = json.loads(run["snapshot_json"])
            intent = required(records, "stage_intents", id=stage["id"])
            thread_id = intent["thread_id"]
            if stage["state"] in {"completed", "waiting"}:
                return self._saved_result(intent, saver)
        scope = self.stages.scope(stage["id"], clock=services.clock)
        scope.preflight(token)
        keys = (
            ("processor", "reviewer")
            if plan.mode in {"implement", "reproduce"}
            else (plan.role_key,)
        )
        services.roles.admit(token, now=services.clock(), keys=keys)
        code = CodeVersion.model_validate(
            plan.code_version or snapshot["configuration"]["code_version"]
        )
        selection = snapshot["roles"][plan.role_key]
        if selection["role"] == "reviewer" and plan.mode != "review":
            raise PermissionError("独审模型只能进入只读独审模式")
        with services.store.read() as records:
            provider = required(records, "provider_revisions", id=selection["provider_revision_id"])
        calls = ModelCalls(
            services.store,
            services.budgets,
            services.queue,
            services.artifacts,
            clock=services.clock,
            wait=self.wait,
            roles=services.roles,
            safety=scope,
        )
        limits = ModelLimits(**snapshot["configuration"].get("model_request_limits", {}))
        retry = RetryPolicy(**snapshot["configuration"].get("retry_policy", {}))
        events = []
        try:
            with managed_role_model(
                services.adapter,
                calls,
                token,
                plan.role_key,
                stage["id"],
                limits=limits,
                policy=retry,
            ) as model:
                reservation_id = model.preallocate()
                envelope = TaskEnvelope(
                    context=RunContext(
                        repository_id=str(required_id(snapshot, run)),
                        task_id=run["task_id"],
                        run_id=run["id"],
                        input_revision=run["input_revision"],
                        acceptance_revision=snapshot["acceptance_revision"],
                        configuration_id=provider["id"],
                        configuration_revision=provider["revision"],
                        execution_generation=token.generation,
                        code=code,
                    ),
                    role=selection["role"],
                    mode=plan.mode,
                    objective=plan.objective,
                    budget=BudgetReference(
                        owner_task_id=run["budget_owner_task_id"],
                        ledger_revision=services.budgets.view(run["budget_owner_task_id"])[
                            "revision"
                        ],
                        reservation_id=reservation_id,
                    ),
                    tool_policy=ToolPolicy(
                        mode=plan.mode, allowed=tuple(item.tool.name for item in plan.tools)
                    ),
                    parent_run_id=run["parent_run_id"],
                )
                children = []
                for child in plan.children:
                    if not {item.tool.name for item in child.tools} <= set(
                        envelope.tool_policy.allowed
                    ):
                        raise PermissionError("子任务工具不能扩大父任务授权")

                    def invoke_child(_state, config, child=child):
                        child_id = stable_id("child-run", token.run_id, plan.key, child.key)
                        with services.store.read() as records:
                            old = records.one("runs", id=child_id)
                            completed = (
                                records.find("stage_intents", run_id=child_id) if old else []
                            )
                        if old and old["state"] == "ended" and completed:
                            value = self._saved_result(completed[-1], saver)
                        else:
                            child_token = self.delegation.start(
                                token,
                                child_id,
                                mode=child.mode,
                                role_key=child.role_key,
                                now=services.clock(),
                            )
                            try:
                                value = self._run(child_token, child, saver, gate)
                            finally:
                                services.queue.complete(
                                    child_token, now=services.clock(), outcome="child_returned"
                                )
                        return {
                            "messages": [AIMessage(content=value.model_dump_json())],
                            "structured_response": value.model_dump(mode="json"),
                        }

                    children.append(
                        {
                            "name": child.key,
                            "description": child.objective,
                            "runnable": RunnableLambda(invoke_child),
                        }
                    )
                executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)
                boundary = RuntimeBoundary(
                    envelope,
                    token,
                    scope,
                    gate,
                    executor,
                    {item.tool.name: item for item in plan.tools},
                    {item.key for item in plan.children},
                    events,
                )
                graph = create_deep_agent(
                    model=model,
                    tools=[item.tool for item in plan.tools],
                    backend=StateBackend(),
                    middleware=[boundary],
                    subagents=children,
                    response_format=ToolStrategy(StructuredResult, handle_errors=False),
                    checkpointer=saver,
                    system_prompt=system_prompt,
                )
                config = {
                    "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
                    "max_concurrency": 1,
                    "recursion_limit": 256,
                }
                state = graph.invoke(
                    {"messages": [{"role": "user", "content": envelope.model_dump_json()}]},
                    config=config,
                )
                raw = state.get("structured_response")
                if isinstance(raw, StructuredResult):
                    raw = raw.model_dump(mode="json")
                if not isinstance(raw, dict):
                    raise MissingStructuredResult()
                result = validate_result(envelope, DEFAULT_REDACTOR.clean(raw))
                for item in result.artifacts:
                    artifact = services.artifacts.require(
                        item.artifact_id, run_id=token.run_id, now=services.clock()
                    )
                    if (
                        artifact["sha256"] != item.sha256
                        or artifact["relative_path"] != item.relative_path
                        or (services.artifacts.root / artifact["relative_path"]).stat().st_size
                        != item.size_bytes
                    ):
                        raise PermissionError("交接引用与真实不可变产物不匹配")
                verified = plan.verifier(result, tuple(events))
                observation = {
                    "producer": "host_stage_verifier",
                    "objective_key": plan.objective_key,
                    "status": "waiting" if result.status == "waiting" else "completed",
                    **verified,
                }
                observation["producer"], observation["objective_key"] = (
                    "host_stage_verifier",
                    plan.objective_key,
                )
                if result.status == "waiting":
                    observation["status"] = "waiting"
                evidence_id = services.artifacts.register(
                    token,
                    "stage-proof:" + stage["id"],
                    canonical_json(observation).encode(),
                    context={
                        "producer": "host_stage_verifier",
                        "objective_key": plan.objective_key,
                    },
                    now=services.clock(),
                )
                self.progress.record(token, plan.objective_key, evidence_id, now=services.clock())
                output_id = services.artifacts.register(
                    token,
                    "stage-result:" + stage["id"],
                    result.model_dump_json().encode(),
                    context={"producer": "host_runtime"},
                    now=services.clock(),
                )
                checkpoint = saver.get_tuple(config)
                if checkpoint is None:
                    raise StorageError("图检查点缺失，不能确认阶段结果")
                self.stages.finish(
                    token,
                    stage["id"],
                    {"structured_result": result.model_dump(mode="json")},
                    [output_id, evidence_id, *[item.artifact_id for item in result.artifacts]],
                    checkpoint_id=checkpoint.checkpoint["id"],
                    now=services.clock(),
                )
                if result.status == "blocked":
                    with services.store.write() as records:
                        block(
                            records,
                            "runtime_reported_blocked",
                            run["task_id"],
                            now=services.clock(),
                            stage_id=stage["id"],
                            unresolved=result.unresolved,
                        )
                return result
        except BudgetDenied:
            self._cancel_unstarted(token)
            with services.store.write() as records:
                records.change("runtime_stages", {"id": stage["id"]}, {"state": "waiting"})
            raise
        except (StorageError, sqlite3.Error):
            services.store.stop_execution()
            try:
                with services.store.write() as records:
                    block(
                        records,
                        "storage_failure",
                        run["task_id"],
                        now=services.clock(),
                        stage_id=stage["id"],
                    )
            except StorageError:
                pass
            raise StorageError("图状态或必要证据无法持久化，停止新执行") from None
        except MissingStructuredResult:
            self._cancel_unstarted(token)
            if plan.mode == "scan":
                verified = plan.verifier(None, tuple(events))
                evidence_id = services.artifacts.register(
                    token,
                    "stage-incomplete:" + stage["id"],
                    canonical_json(
                        {
                            **verified,
                            "producer": "host_stage_verifier",
                            "objective_key": plan.objective_key,
                            "status": "completed",
                        }
                    ).encode(),
                    context={
                        "producer": "host_stage_verifier",
                        "objective_key": plan.objective_key,
                    },
                    now=services.clock(),
                )
                self.progress.record(token, plan.objective_key, evidence_id, now=services.clock())
            with services.store.write() as records:
                records.change("runtime_stages", {"id": stage["id"]}, {"state": "failed"})
                if plan.mode != "scan":
                    block(
                        records,
                        "runtime_execution_rejected",
                        run["task_id"],
                        now=services.clock(),
                        stage_id=stage["id"],
                    )
            raise
        except (ValueError, PermissionError, ProviderRequestError):
            self._cancel_unstarted(token)
            with services.store.write() as records:
                block(
                    records,
                    "runtime_execution_rejected",
                    run["task_id"],
                    now=services.clock(),
                    stage_id=stage["id"],
                )
                records.change("runtime_stages", {"id": stage["id"]}, {"state": "failed"})
            raise

    def _saved_result(self, intent, saver):
        if intent["state"] != "checkpoint_saved" or not intent["checkpoint_id"]:
            raise PermissionError("旧阶段缺少确认，必须先核对结果")
        try:
            checkpoint = saver.get_tuple(
                {
                    "configurable": {
                        "thread_id": intent["thread_id"],
                        "checkpoint_ns": "",
                        "checkpoint_id": intent["checkpoint_id"],
                    }
                }
            )
        except sqlite3.Error:
            checkpoint = None
        if checkpoint is None:
            self.services.store.stop_execution()
            raise StorageError("已确认阶段的检查点缺失，停止新执行并等待核对")
        payload = json.loads(intent["result_json"])
        for item in payload["artifact_ids"]:
            self.services.artifacts.require(
                item, run_id=intent["run_id"], now=self.services.clock()
            )
        return StructuredResult.model_validate(payload["result"]["structured_result"])

    def _cancel_unstarted(self, token):
        with self.services.store.read() as records:
            reserved = [
                row["id"]
                for row in records.find(
                    "budget_reservations", run_id=token.run_id, state="reserved"
                )
            ]
        for identifier in reserved:
            self.services.budgets.cancel_unstarted(token, identifier, now=self.services.clock())


def required_id(snapshot, run):
    # A real repository identity must be pinned; global diagnostics use their typed handler.
    repository_id = snapshot.get("repository_id")
    if not repository_id:
        raise PermissionError("运行缺少已确认的仓库身份")
    return repository_id
