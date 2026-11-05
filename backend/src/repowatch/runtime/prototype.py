import threading
from collections.abc import Sequence
from contextlib import contextmanager

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool

from repowatch.domain.protocols import StructuredResult, TaskEnvelope, validate_result


class SerialActivity:
    def __init__(self, max_steps: int = 32):
        if max_steps < 1:
            raise ValueError("工具与模型步骤上限必须为正")
        self._lock = threading.Lock()
        self.steps = 0
        self.max_steps = max_steps

    @contextmanager
    def enter(self):
        if not self._lock.acquire(blocking=False):
            raise PermissionError("父子模型及工具不得并行执行")
        try:
            if self.steps >= self.max_steps:
                raise PermissionError("P0 切片达到独立步骤上限")
            self.steps += 1
            yield
        finally:
            self._lock.release()


class MissingStructuredResult(ValueError):
    def __init__(self, *, final_tool_names=(), final_text_length=0):
        super().__init__("缺少结构化交接结果")
        self.final_tool_names = tuple(final_tool_names)
        self.final_text_length = final_text_length


def text_length(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return 0


class ToolBoundary(AgentMiddleware):
    def __init__(self, envelope: TaskEnvelope, activity: SerialActivity, child_name: str | None):
        self.envelope = envelope
        self.activity = activity
        self.child_name = child_name

    def wrap_model_call(self, request, handler):
        allowed_names = set(self.envelope.tool_policy.allowed)
        if self.child_name:
            allowed_names.add("task")
        tools = [tool for tool in request.tools if getattr(tool, "name", None) in allowed_names]
        with self.activity.enter():
            return handler(request.override(tools=tools))

    def wrap_tool_call(self, request, handler):
        tool_name = request.tool_call["name"]
        if tool_name == "task":
            if (
                self.child_name is None
                or request.tool_call["args"].get("subagent_type") != self.child_name
            ):
                raise PermissionError("只允许预先绑定的同步子任务")
            return handler(request)
        self.envelope.tool_policy.require(tool_name)
        with self.activity.enter():
            return handler(request)


class LocalRuntimePrototype:
    def __init__(self):
        self._execution = threading.Lock()

    def run(
        self,
        envelope: TaskEnvelope,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        *,
        child: tuple[TaskEnvelope, BaseChatModel, Sequence[BaseTool]] | None = None,
        max_steps: int = 32,
        system_prompt: str | None = None,
    ) -> StructuredResult:
        if not self._execution.acquire(blocking=False):
            raise PermissionError("本地切片已有执行者")
        try:
            activity = SerialActivity(max_steps)
            subagents = []
            child_name = None
            if child:
                child_envelope, child_model, child_tools = child
                self._validate_child(envelope, child_envelope)
                child_name = "bounded-child"
                child_graph = self._build(child_envelope, child_model, child_tools, activity)

                def invoke_child(state, config):
                    result = child_graph.invoke(
                        {
                            "messages": [
                                {"role": "user", "content": child_envelope.model_dump_json()}
                            ]
                        },
                        config={**config, "max_concurrency": 1, "recursion_limit": 40},
                    )
                    validated = self._result(child_envelope, result)
                    return {
                        "messages": result["messages"],
                        "structured_response": validated.model_dump(mode="json"),
                    }

                subagents = [
                    {
                        "name": child_name,
                        "description": child_envelope.objective,
                        "runnable": RunnableLambda(invoke_child),
                    }
                ]
            graph = self._build(
                envelope,
                model,
                tools,
                activity,
                child_name,
                subagents,
                system_prompt=system_prompt,
            )
            state = graph.invoke(
                {"messages": [{"role": "user", "content": envelope.model_dump_json()}]},
                config={"max_concurrency": 1, "recursion_limit": 40},
            )
            return self._result(envelope, state)
        finally:
            self._execution.release()

    @staticmethod
    def _validate_child(parent: TaskEnvelope, child: TaskEnvelope) -> None:
        parent_context = parent.context.model_copy(
            update={
                "run_id": child.context.run_id,
                "configuration_id": child.context.configuration_id,
                "configuration_revision": child.context.configuration_revision,
            }
        )
        if (
            child.parent_run_id != parent.context.run_id
            or child.context != parent_context
            or child.budget != parent.budget
            or not set(child.tool_policy.allowed) <= set(parent.tool_policy.allowed)
            or child.role != "processor"
        ):
            raise PermissionError("子任务归属、预算或权限不匹配")

    @staticmethod
    def _result(envelope: TaskEnvelope, state: dict) -> StructuredResult:
        result = state.get("structured_response")
        if isinstance(result, StructuredResult):
            result = result.model_dump()
        if not isinstance(result, dict):
            messages = state.get("messages")
            final = messages[-1] if isinstance(messages, list) and messages else None
            tool_calls = getattr(final, "tool_calls", None)
            content = getattr(final, "content", None)
            raise MissingStructuredResult(
                final_tool_names=tuple(
                    call["name"]
                    for call in tool_calls or ()
                    if isinstance(call, dict) and isinstance(call.get("name"), str)
                ),
                final_text_length=text_length(content),
            )
        return validate_result(envelope, result)

    @staticmethod
    def _build(
        envelope: TaskEnvelope,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        activity: SerialActivity,
        child_name: str | None = None,
        subagents: list | None = None,
        *,
        system_prompt: str | None = None,
    ):
        for tool in tools:
            envelope.tool_policy.require(tool.name)
        return create_deep_agent(
            model=model,
            system_prompt=system_prompt,
            tools=list(tools),
            backend=StateBackend(),
            subagents=subagents or [],
            middleware=[ToolBoundary(envelope, activity, child_name)],
            response_format=ToolStrategy(StructuredResult, handle_errors=False),
        )
