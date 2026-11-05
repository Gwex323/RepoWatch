"""OpenAI 兼容网关的显式协议适配；不自动选择模型或重试。"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import openai
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from pydantic import Field

from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.task_records import business_event
from repowatch.domain.providers import ProviderModel
from repowatch.security import DEFAULT_REDACTOR

ADAPTER_REVISION = 1


class ProviderRequestError(RuntimeError):
    def __init__(self, code, *, status_code=None, transient=False):
        self.code, self.status_code, self.transient = code, status_code, transient
        super().__init__("供应商请求失败：" + code)


def safe_failure(error):
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    code = body.get("code") if isinstance(body, dict) else None
    if code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}:
        return ProviderRequestError("quota_exhausted", status_code=status)
    return ProviderRequestError(
        failure_code(error),
        status_code=status,
        transient=status in {408, 409, 429, 500, 502, 503, 504}
        or isinstance(error, (openai.APIConnectionError, httpx.TransportError)),
    )


def token_parameter(base_url, configured=None):
    return configured or (
        "max_completion_tokens" if urlsplit(base_url).hostname == "api.openai.com" else "max_tokens"
    )


class GatewayChatModel(ChatOpenAI):
    chat_token_parameter: str = Field(default="max_tokens", exclude=True)
    configured_tool_choice: str = Field(default="auto", exclude=True)

    def _tool_choice(self):
        return self.configured_tool_choice

    def _generate(self, *args, **kwargs):
        try:
            return super()._generate(*args, **kwargs)
        except (openai.APIError, httpx.HTTPError) as error:
            raise safe_failure(error) from None

    async def _agenerate(self, *args, **kwargs):
        raise PermissionError("当前正式适配只允许受管同步调用")

    def _stream(self, *args, **kwargs):
        raise PermissionError("当前正式适配尚未开放流式请求")

    async def _astream(self, *args, **kwargs):
        raise PermissionError("当前正式适配尚未开放流式请求")

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        if kwargs.get("previous_response_id") or kwargs.get("background"):
            raise PermissionError("受管运行不使用供应商后台任务或跨会话隐式上下文")
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if not self.use_responses_api and self.chat_token_parameter == "max_tokens":
            if "max_completion_tokens" in payload:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
        if payload.get("tool_choice") == "required":
            payload["tool_choice"] = self._tool_choice()
        if payload.get("tools"):
            if any(tool.get("type") != "function" for tool in payload["tools"]):
                raise PermissionError("仅允许 Harness 注册的本地函数工具")
            payload["parallel_tool_calls"] = False
        payload["store"] = False
        payload["model"] = self.model_name
        payload["max_output_tokens" if self.use_responses_api else self.chat_token_parameter] = (
            self.max_tokens
        )
        return payload


@dataclass(frozen=True)
class ModelLimits:
    timeout_seconds: float = 30
    max_output_tokens: int = 2048

    def __post_init__(self):
        if (
            not 0 < self.timeout_seconds <= 120
            or type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 4096
        ):
            raise ValueError("模型请求必须具有有限超时和输出上限")


def failure_code(error):
    status = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if status in {401, 403}:
        return "authentication_or_permission"
    if status == 429:
        return "rate_or_quota_limit"
    if status is not None:
        return "provider_http_error"
    if "timeout" in type(error).__name__.lower():
        return "timeout"
    return "provider_or_protocol_error"


class ProviderAdapter:
    def __init__(self, service, *, transport=None):
        self.service, self.transport = service, transport

    @contextmanager
    def session(self, revision_id, selection: ProviderModel, limits=None, *, callbacks=None):
        limits = limits or ModelLimits()
        revision = self.service.revision(revision_id)
        configuration = json.loads(revision["configuration_json"])
        secret = self.service.vault.read(revision["credential_reference"])
        with httpx.Client(
            transport=self.transport,
            timeout=limits.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            model = GatewayChatModel(
                model=selection.model,
                api_key=secret.reveal(),
                base_url=configuration["base_url"],
                use_responses_api=configuration["api"] == "responses",
                use_previous_response_id=False,
                max_retries=0,
                timeout=limits.timeout_seconds,
                max_tokens=limits.max_output_tokens,
                reasoning_effort=selection.reasoning_effort,
                store=False,
                http_client=client,
                openai_proxy="",
                callbacks=callbacks or [],
                chat_token_parameter=token_parameter(
                    configuration["base_url"],
                    configuration.get("chat_token_parameter"),
                ),
                configured_tool_choice=configuration.get("tool_choice", "auto"),
            )
            with tracing_context(enabled=False):
                yield model

    def list_models(self, operation_id, revision_id, *, actor="local-user"):
        human(actor)
        request = {"kind": "model_list", "revision_id": revision_id, "actor": actor}
        with self.service.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                # A previous unconfirmed request is not silently sent again.
                return replay
            remember_operation(
                records,
                operation_id,
                fingerprint,
                1,
                {
                    "models": [],
                    "available": False,
                    "error": "result_unknown",
                },
            )
            business_event(records, "model_list_requested", operation_id=operation_id)
        revision = self.service.revision(revision_id)
        configuration = json.loads(revision["configuration_json"])
        result = {"models": [], "available": False, "error": None}
        try:
            secret = self.service.vault.read(revision["credential_reference"])
            with httpx.Client(
                transport=self.transport,
                timeout=15,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    "GET",
                    configuration["base_url"] + "/models",
                    headers={"Authorization": "Bearer " + secret.reveal()},
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError("模型列表过大")
                body = json.loads(raw)
            rows = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(rows, list)
                or len(rows) > 1000
                or any(
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not 1 <= len(row["id"]) <= 200
                    for row in rows
                )
            ):
                raise ValueError("模型列表响应无效")
            result.update(
                models=sorted({DEFAULT_REDACTOR.text(row["id"]) for row in rows}), available=True
            )
        except Exception as error:
            result["error"] = failure_code(error)
        with self.service.store.write() as records:
            records.change(
                "user_operations", {"id": operation_id}, {"result_json": json.dumps(result)}
            )
            business_event(records, "model_list_finished", operation_id=operation_id, **result)
        return result
