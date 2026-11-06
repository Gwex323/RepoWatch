"""LangChain 模型包装：工具绑定和结构化输出均经过同一预算／重试入口。"""

import json
from contextlib import contextmanager
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import message_to_dict, messages_from_dict, messages_to_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr

from repowatch.adapters.providers import ModelLimits
from repowatch.application.provider_calls import RetryPolicy
from repowatch.domain.budget import BudgetAmounts, BudgetDenied
from repowatch.domain.providers import ProviderModel
from repowatch.domain.task_records import canonical_json, stable_id
from repowatch.security import DEFAULT_REDACTOR


def encode_result(result):
    return canonical_json(
        DEFAULT_REDACTOR.clean(
            {
                "generations": [
                    {"message": message_to_dict(item.message), "info": item.generation_info}
                    for item in result.generations
                ],
            }
        )
    ).encode()


def decode_result(raw):
    data = json.loads(raw)
    return ChatResult(
        generations=[
            ChatGeneration(
                message=messages_from_dict([item["message"]])[0],
                generation_info=item["info"],
            )
            for item in data["generations"]
        ]
    )


def result_usage(result):
    actual = {"model_calls": 1}
    if len(result.generations) == 1:
        value = (result.generations[0].message.usage_metadata or {}).get("total_tokens")
        if type(value) is int and value >= 0:
            actual["tokens"] = value
    return actual


class BudgetedChatModel(BaseChatModel):
    delegate: BaseChatModel = Field(exclude=True)
    calls: Any = Field(exclude=True)
    token: Any = Field(exclude=True)
    role_key: str
    namespace: str
    upper: BudgetAmounts
    policy: RetryPolicy = Field(default_factory=RetryPolicy)
    max_input_tokens: int
    _next_index: int = PrivateAttr(default=0)
    _preallocated_id: str | None = PrivateAttr(default=None)

    def preallocate(self):
        identifier = stable_id("model-call", self.token.run_id, f"{self.namespace}:0")
        decision = self.calls.executor.budgets.reserve(
            self.token,
            f"model:{identifier}:1",
            self.upper,
            purpose="model",
            now=self.calls.clock(),
        )
        if not decision.allowed:
            raise BudgetDenied(decision)
        self._preallocated_id = decision.reservation_id
        return decision.reservation_id

    @property
    def _llm_type(self):
        return "repowatch-budgeted-model"

    @property
    def _identifying_params(self):
        return {"role": self.role_key, "namespace": self.namespace}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        converted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=converted, tool_choice=tool_choice or "auto", **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        request = {"messages": messages_to_dict(messages), "stop": stop, "parameters": kwargs}
        if len(canonical_json(request).encode()) + 4096 > self.max_input_tokens:
            raise PermissionError("模型上下文超过已解析的单步输入上限")
        call_key = f"{self.namespace}:{self._next_index}"
        preallocated_id = self._preallocated_id if self._next_index == 0 else None
        self._next_index += 1
        return self.calls.invoke(
            self.token,
            call_key,
            request,
            self.upper,
            lambda: self.delegate._generate(messages, stop=stop, **kwargs),
            result_usage,
            encode_result,
            decode_result,
            role_key=self.role_key,
            policy=self.policy,
            preallocated_id=preallocated_id,
        )

    async def _agenerate(self, *args, **kwargs):
        raise PermissionError("受管模型当前只允许同步串行调用")


@contextmanager
def managed_role_model(
    adapter,
    calls,
    token,
    role_key,
    namespace,
    *,
    limits=None,
    policy=None,
    max_input_tokens=16000,
):
    limits, policy = limits or ModelLimits(), policy or RetryPolicy()
    with calls.store.read() as records:
        snapshot = json.loads(records.one("runs", id=token.run_id)["snapshot_json"])
        resolved = snapshot.get("roles", {}).get(role_key)
    configured = snapshot.get("configuration", {}).get("max_input_tokens")
    if configured is not None:
        max_input_tokens = configured
    if type(max_input_tokens) is not int or not 4096 <= max_input_tokens <= 100000:
        raise ValueError("单步输入上限必须是有限的安全整数")
    if not resolved:
        raise PermissionError("当前运行尚未固化指定角色配置")
    with adapter.session(
        resolved["provider_revision_id"],
        ProviderModel.model_validate(resolved["selection"]),
        limits,
    ) as delegate:
        yield BudgetedChatModel(
            delegate=delegate,
            calls=calls,
            token=token,
            role_key=role_key,
            namespace=namespace,
            upper=BudgetAmounts(
                active_ms=round(limits.timeout_seconds * 1000),
                model_calls=1,
                tokens=max_input_tokens + limits.max_output_tokens,
            ),
            policy=policy,
            max_input_tokens=max_input_tokens,
        )
