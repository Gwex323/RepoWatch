import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langsmith import tracing_context
from pydantic import Field, ValidationError, field_validator, model_validator

from repowatch.adapters.providers import GatewayChatModel, token_parameter
from repowatch.domain.protocols import Contract, Identifier, TaskEnvelope
from repowatch.runtime.prototype import LocalRuntimePrototype, MissingStructuredResult

BROKEN = "def add(left, right):\n    return left - right\n"
FIXED = "def add(left, right):\n    return left + right\n"
APIS = ("chat_completions", "responses")
ROLES = ("processor", "reviewer")
HANDOFF_SYSTEM_PROMPT = (
    "You are running an in-memory API capability fixture. The user's JSON is a task envelope. "
    "Follow its objective and tool policy. After executing every required fixture tool, call "
    "StructuredResult with the envelope's exact context and mode, status='completed', "
    "artifacts=[], and unresolved=[]. The summary field is an opaque challenge response, "
    "not a description of your work: copy the handoff_summary string returned by a fixture "
    "tool verbatim, without quotes, Markdown, whitespace, translation, or extra explanation. "
    "Never invent or modify that string. Do not finish before the required tool activities."
)
REQUEST_PARAMETERS = {
    "model",
    "tools",
    "tool_choice",
    "response_format",
    "parallel_tool_calls",
    "reasoning",
    "reasoning_effort",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "store",
    "input",
    "messages",
    "text",
    "stream",
}
PROVIDER_CODES = {
    "BadRequest",
    "InvalidParameter",
    "InvalidParameterValue",
    "InvalidRequest",
    "UnsupportedParameter",
    "model_not_found",
    "no_available_channel",
    "channel_not_found",
    "invalid_request_error",
    "new_api_error",
}


class ProbeModel(Contract):
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=30)


class ProbeProvider(Contract):
    name: Identifier
    base_url: str = Field(min_length=1, max_length=500)
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    credential_revision: int = Field(default=1, strict=True, ge=1)
    configuration_revision: int = Field(default=1, strict=True, ge=1)
    processor: ProbeModel
    reviewer: ProbeModel
    chat_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    tool_choice: Literal["auto", "required"] = "auto"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("地址不能包含凭据、查询参数或片段")
        return value.rstrip("/")


class ProbeLimits(Contract):
    max_calls: int = Field(default=32, strict=True, ge=1, le=64)
    max_calls_per_cell: int = Field(default=4, strict=True, ge=1, le=8)
    max_output_tokens: int = Field(default=2048, strict=True, ge=256, le=4096)
    request_timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    total_seconds: float = Field(default=300, gt=0, le=1800, allow_inf_nan=False)


class ProbeConfiguration(Contract):
    protocol_version: str = Field(default="1", pattern=r"^1$")
    providers: tuple[ProbeProvider, ...] = Field(min_length=2, max_length=2)
    limits: ProbeLimits = Field(default_factory=ProbeLimits)

    @model_validator(mode="after")
    def validate_names(self):
        if len({provider.name.casefold() for provider in self.providers}) != 2:
            raise ValueError("供应商名称必须唯一")
        return self


class ProbeSelection(Contract):
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    api: Literal["chat_completions", "responses"] | None = None
    role: Literal["processor", "reviewer"] | None = None
    stage: Literal["connection", "tools", "handoff"] = "handoff"

    @property
    def diagnostic_only(self) -> bool:
        return self.stage != "handoff" or any((self.provider, self.api, self.role))

    def matches(self, provider, api, role) -> bool:
        return (
            (self.provider is None or self.provider == provider.name)
            and (self.api is None or self.api == api)
            and (self.role is None or self.role == role)
        )


class ProbeLimitExceeded(RuntimeError):
    pass


class ProbeCapabilityError(RuntimeError):
    pass


class ProbeChatModel(GatewayChatModel):
    probe_tool_choice: str = Field(default="auto", exclude=True)

    def _tool_choice(self):
        return self.probe_tool_choice


def chat_token_parameter(provider: ProbeProvider) -> str:
    return token_parameter(provider.base_url, provider.chat_token_parameter)


class ProbeMeter(BaseCallbackHandler):
    raise_error = True

    def __init__(self, limits: ProbeLimits, shared: dict):
        self.limits = limits
        self.shared = shared
        self.calls = 0
        self.usages: list[dict | None] = []
        self.served_models: set[str] = set()
        self.http_requests_started = 0
        self.http_statuses: list[int] = []
        self.request_ids: set[str] = set()
        self.provider_codes: set[str] = set()
        self.rejected_parameters: set[str] = set()
        self.error_hints: set[str] = set()
        self.output_truncated = False

    def on_http_request(self, request):
        self.http_requests_started += 1

    def on_http_response(self, response):
        self.http_statuses.append(response.status_code)
        for header in ("x-request-id", "x-tt-logid", "request-id"):
            value = response.headers.get(header, "")
            if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
                self.request_ids.add(value)
        response.read()
        try:
            body = response.json()
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if response.is_success:
            choices = body.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else None
            self.output_truncated |= (
                isinstance(first, dict) and first.get("finish_reason") == "length"
            ) or (
                body.get("status") == "incomplete"
                and isinstance(body.get("incomplete_details"), dict)
                and body["incomplete_details"].get("reason") == "max_output_tokens"
            )
            return
        error = body.get("error")
        if not isinstance(error, dict):
            return
        code = error.get("code")
        if isinstance(code, str) and code in PROVIDER_CODES:
            self.provider_codes.add(code)
        parameter = error.get("param")
        if isinstance(parameter, str):
            parameter = parameter.split(".", 1)[0].split("[", 1)[0]
            if parameter in REQUEST_PARAMETERS:
                self.rejected_parameters.add(parameter)
        message = error.get("message")
        if isinstance(message, str):
            request_id = re.search(
                r"request id:\s*([A-Za-z0-9_-]{8,128})(?![A-Za-z0-9_-])", message, re.I
            )
            if request_id:
                self.request_ids.add(request_id.group(1))
            normalized = message.casefold()
            if any(
                text in normalized
                for text in ("no available channel", "无可用渠道", "没有可用渠道")
            ):
                self.error_hints.add("no_available_channel")
            if any(text in normalized for text in ("model not found", "模型不存在")):
                self.error_hints.add("model_not_found")

    def diagnostics(self) -> dict:
        return {
            "requests_started": self.http_requests_started,
            "responses_received": len(self.http_statuses),
            "successful_responses": sum(200 <= status < 300 for status in self.http_statuses),
            "status_codes": self.http_statuses,
            "request_ids": sorted(self.request_ids),
            "provider_codes": sorted(self.provider_codes),
            "rejected_parameters": sorted(self.rejected_parameters),
            "error_hints": sorted(self.error_hints),
            "output_truncated": self.output_truncated,
        }

    def on_chat_model_start(self, serialized, messages, **kwargs):
        if (
            self.calls >= self.limits.max_calls_per_cell
            or self.shared["calls"] >= self.limits.max_calls
            or time.monotonic() >= self.shared["deadline"]
        ):
            raise ProbeLimitExceeded("E1 调用或时间上限已到达")
        self.calls += 1
        self.shared["calls"] += 1
        self.usages.append(None)

    def on_llm_end(self, response, **kwargs):
        message = response.generations[0][0].message
        served = message.response_metadata.get("model_name")
        if isinstance(served, str):
            self.served_models.add(served)
        usage = getattr(message, "usage_metadata", None)
        if usage is not None:
            fields = (usage.get("input_tokens"), usage.get("output_tokens"))
            if all(type(count) is int and count >= 0 for count in fields):
                self.usages[-1] = {"input_tokens": fields[0], "output_tokens": fields[1]}

    def summary(self) -> dict:
        known = [usage for usage in self.usages if usage is not None]
        unknown = len(self.usages) - len(known)
        return {
            "calls": self.calls,
            "reported_input_tokens": sum(usage["input_tokens"] for usage in known),
            "reported_output_tokens": sum(usage["output_tokens"] for usage in known),
            "unknown_usage_calls": unknown,
            "served_models": sorted(self.served_models),
            "actual_cost": None,
            "cost_note": "未读取供应商账单；费用待核实，缺失用量保留未知占用",
        }


def probe_envelope(provider: ProbeProvider, api: str, role: str, session: str) -> TaskEnvelope:
    mode = "implement" if role == "processor" else "review"
    allowed = (
        ["read_source", "write_source"]
        if role == "processor"
        else [
            "read_source",
            "read_evidence",
        ]
    )
    return TaskEnvelope.model_validate(
        {
            "context": {
                "repository_id": "e1-fixture",
                "task_id": f"e1-{session}",
                "run_id": f"{api}-{role}-{session}",
                "input_revision": 1,
                "acceptance_revision": 1,
                "configuration_id": provider.name,
                "configuration_revision": provider.configuration_revision,
                "execution_generation": 1,
                "code": {
                    "base_sha": "a" * 40,
                    "candidate_sha": "b" * 40,
                    "tree_sha": "c" * 40,
                    "content_manifest_sha256": "d" * 64,
                },
            },
            "role": role,
            "mode": mode,
            "objective": (
                "这是内存能力夹具，代码和 Git 标识均为模拟值。"
                "先调用 read_source(path='fixture.py')。处理角色须用 write_source 将减法改为加法，"
                "审查角色须调用 read_evidence 读取证据。最终调用 StructuredResult 返回当前信封的"
                "完整 context 和 mode，status=completed，artifacts=[]，unresolved=[]，"
                "summary 必须逐字复制工具返回的 handoff_summary 字符串，即 probe: 后接 proof 原文。"
                "summary 不填写任务说明，不加引号、空格或 Markdown。不得调用其他工具。"
            ),
            "budget": {
                "owner_task_id": f"e1-{session}",
                "ledger_revision": 1,
                "reservation_id": f"reserve-{session}",
            },
            "tool_policy": {"mode": mode, "allowed": allowed},
        }
    )


def fixture_tools(role: str):
    state = {
        "content": BROKEN if role == "processor" else FIXED,
        "proof": secrets.token_hex(16),
        "tools": [],
    }

    @tool(description="只读取内存夹具 fixture.py；handoff_summary 供最终 summary 逐字复制")
    def read_source(path: str) -> str:
        if path != "fixture.py":
            raise ProbeCapabilityError("夹具路径无效")
        state["tools"].append("read_source")
        return json.dumps(
            {
                "content": state["content"],
                "proof": state["proof"],
                "handoff_summary": f"probe:{state['proof']}",
            }
        )

    @tool(description="将 add 改成 left + right，提交完整文件；返回的 handoff_summary 必须逐字回传")
    def write_source(content: str) -> str:
        if "read_source" not in state["tools"] or content != FIXED:
            raise ProbeCapabilityError("夹具修改未满足要求")
        state["content"] = content
        state["tools"].append("write_source")
        return json.dumps(
            {
                "written": True,
                "proof": state["proof"],
                "handoff_summary": f"probe:{state['proof']}",
            }
        )

    @tool(description="读取只读模拟前后证据；返回的 handoff_summary 必须逐字回传，不运行命令")
    def read_evidence() -> str:
        state["tools"].append("read_evidence")
        return json.dumps(
            {
                "base_result": -1,
                "candidate_result": 3,
                "proof": state["proof"],
                "handoff_summary": f"probe:{state['proof']}",
            }
        )

    return state, [read_source, write_source] if role == "processor" else [
        read_source,
        read_evidence,
    ]


def error_code(error: Exception) -> str:
    if isinstance(error, ProbeLimitExceeded):
        return "budget_exhausted"
    if isinstance(error, ProbeCapabilityError | PermissionError):
        return "capability_rejected"
    if isinstance(error, ValidationError | ValueError):
        return "invalid_handoff"
    status = http_status(error)
    if status in {401, 403}:
        return "authentication_or_permission"
    if status == 429:
        return "rate_or_quota_limit"
    if status is not None:
        return "provider_http_error"
    if "timeout" in type(error).__name__.lower():
        return "timeout"
    return "provider_or_protocol_error"


def http_status(error: Exception) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    value = getattr(error, "status_code", None)
    return value if type(value) is int else None


def run_connection(provider, api, role, credential, client, meter):
    selection = getattr(provider, role)
    payload = {"model": selection.model, "stream": False}
    if api == "responses":
        operation = "responses"
        payload.update(input="Reply with OK.", max_output_tokens=32, store=False)
    else:
        operation = "chat/completions"
        payload.update(messages=[{"role": "user", "content": "Reply with OK."}])
        payload[chat_token_parameter(provider)] = 32
    meter.on_chat_model_start(None, None)
    response = client.post(
        f"{provider.base_url}/{operation}",
        headers={"Authorization": f"Bearer {credential}"},
        json=payload,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("连接响应格式无效")
    usage = body.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens" if api == "responses" else "prompt_tokens")
        output_tokens = usage.get("output_tokens" if api == "responses" else "completion_tokens")
        if all(type(count) is int and count >= 0 for count in (input_tokens, output_tokens)):
            meter.usages[-1] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if isinstance(body.get("model"), str):
        meter.served_models.add(body["model"])
    if api == "responses":
        valid = (
            body.get("object") == "response"
            and body.get("status") in {"completed", "incomplete"}
            and isinstance(body.get("output"), list)
        )
    else:
        choices = body.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        valid = isinstance(message, dict) and message.get("role") == "assistant"
    if not valid:
        raise ValueError("缺少模型连接响应")


def run_tools(envelope, model, tools, fixture, tool_choice):
    tools_by_name = {item.name: item for item in tools}
    request = envelope.model_copy(
        update={
            "objective": (
                "这是内存工具能力夹具。先调用 read_source(path='fixture.py')；"
                "处理角色用 write_source 将减法改为加法，审查角色调用 read_evidence。"
                "必须实际调用工具，不需要 StructuredResult 或最终报告。"
            )
        }
    )
    messages = [HumanMessage(content=request.model_dump_json())]
    bound = model.bind_tools(tools, tool_choice=tool_choice)
    while True:
        answer = bound.invoke(messages)
        messages.append(answer)
        if not answer.tool_calls:
            raise ProbeCapabilityError("missing_required_tool")
        for call in answer.tool_calls:
            envelope.tool_policy.require(call["name"])
            result = tools_by_name[call["name"]].invoke(call["args"])
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
        if set(tools_by_name) <= set(fixture["tools"]) and fixture["content"] == FIXED:
            return


def capability_failure(required: set[str], fixture: dict, result) -> str | None:
    if not required <= set(fixture["tools"]):
        return "missing_required_tool"
    if fixture["content"] != FIXED:
        return "fixture_not_modified"
    if result.status != "completed":
        return "status_not_completed"
    if result.artifacts:
        return "artifacts_not_empty"
    if result.unresolved:
        return "unresolved_not_empty"
    if result.summary != f"probe:{fixture['proof']}":
        return "summary_proof_mismatch"
    return None


def safe_token(value: object, limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9_.\[\]-]", "?", str(value))
    return text[:limit]


def handoff_diagnostics(error: Exception) -> dict:
    if isinstance(error, MissingStructuredResult):
        return {
            "kind": "missing_structured_result",
            "final_tool_names": [safe_token(name, 64) for name in error.final_tool_names[:8]],
            "final_text_length": error.final_text_length,
            "field_count": 0,
            "fields": [],
        }
    if isinstance(error, ValidationError):
        details = error.errors()
        return {
            "kind": "validation",
            "final_tool_names": [],
            "final_text_length": 0,
            "field_count": len(details),
            "fields": [
                f"{safe_token('.'.join(str(part) for part in item.get('loc', ())))}:"
                f"{safe_token(item.get('type', ''), 48)}"
                for item in details[:8]
            ],
        }
    known = str(error) == "交接结果不属于当前授权任务、运行、配置或代码版本"
    return {
        "kind": "context_mismatch" if known else "value",
        "final_tool_names": [],
        "final_text_length": 0,
        "field_count": 0,
        "fields": [],
    }


def run_cell(provider, api, role, credential, limits, shared, client, session, stage="handoff"):
    meter = ProbeMeter(limits, shared)
    envelope = probe_envelope(provider, api, role, session)
    fixture, tools = fixture_tools(role)
    selection = getattr(provider, role)
    started = time.monotonic()
    cell = {
        "provider": provider.name,
        "api": api,
        "role": role,
        "model": selection.model,
        "configuration_revision": provider.configuration_revision,
        "credential_revision": provider.credential_revision,
        "reasoning_effort": selection.reasoning_effort,
        "status": "failed",
        "error": None,
        "http_status": None,
        "failure_detail": None,
        "handoff": None,
        "handoff_diagnostics": None,
        "stage": stage,
        "chat_token_parameter": chat_token_parameter(provider)
        if api == "chat_completions"
        else None,
        "tool_choice": provider.tool_choice if stage != "connection" else None,
    }
    client.event_hooks = {"request": [meter.on_http_request], "response": [meter.on_http_response]}
    try:
        model = ProbeChatModel(
            model=selection.model,
            api_key=credential,
            base_url=provider.base_url,
            use_responses_api=api == "responses",
            use_previous_response_id=False,
            max_retries=0,
            timeout=limits.request_timeout_seconds,
            max_tokens=limits.max_output_tokens,
            reasoning_effort=selection.reasoning_effort,
            store=False,
            http_client=client,
            openai_proxy="",
            callbacks=[meter],
            chat_token_parameter=chat_token_parameter(provider),
            probe_tool_choice=provider.tool_choice,
        )
        with tracing_context(enabled=False):
            if stage == "connection":
                run_connection(provider, api, role, credential, client, meter)
            elif stage == "tools":
                run_tools(envelope, model, tools, fixture, provider.tool_choice)
            else:
                result = LocalRuntimePrototype().run(
                    envelope,
                    model,
                    tools,
                    max_steps=12,
                    system_prompt=HANDOFF_SYSTEM_PROMPT,
                )
                cell["handoff"] = {
                    "summary_exact": result.summary == f"probe:{fixture['proof']}",
                    "summary_contains_proof": fixture["proof"] in result.summary,
                    "summary_has_probe_prefix": result.summary.startswith("probe:"),
                    "summary_has_surrounding_whitespace": result.summary != result.summary.strip(),
                    "summary_length": len(result.summary),
                }
                required = {tool.name for tool in tools}
                failure = capability_failure(required, fixture, result)
                if failure:
                    raise ProbeCapabilityError(failure)
        cell["status"] = "passed"
    except Exception as error:
        cell["error"] = error_code(error)
        cell["http_status"] = http_status(error)
        invalid_handoff = isinstance(error, ValueError)
        if invalid_handoff:
            cell["handoff_diagnostics"] = handoff_diagnostics(error)
        if isinstance(error, ProbeCapabilityError):
            detail = str(error)
            cell["failure_detail"] = (
                detail
                if detail
                in {
                    "missing_required_tool",
                    "fixture_not_modified",
                    "status_not_completed",
                    "artifacts_not_empty",
                    "unresolved_not_empty",
                    "summary_proof_mismatch",
                }
                else "fixture_tool_rejected"
            )
        elif isinstance(error, PermissionError):
            cell["failure_detail"] = "tool_boundary_rejected"
        elif invalid_handoff and not meter.output_truncated:
            cell["failure_detail"] = "invalid_handoff"
        if meter.output_truncated and not isinstance(error, ProbeCapabilityError | PermissionError):
            cell["failure_detail"] = "output_truncated"
    cell.update(
        {
            "usage": meter.summary(),
            "tools_executed": fixture["tools"],
            "duration_seconds": round(time.monotonic() - started, 3),
            "http": meter.diagnostics(),
        }
    )
    return cell


def redact(value, credentials):
    if isinstance(value, str):
        for credential in credentials:
            value = value.replace(credential, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, credentials) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, credentials) for item in value]
    return value


def run_probe(
    configuration: ProbeConfiguration,
    credentials: Mapping[str, str],
    *,
    transport=None,
    selection: ProbeSelection | None = None,
):
    selection = selection or ProbeSelection()
    session = secrets.token_hex(8)
    selected_providers = [
        provider
        for provider in configuration.providers
        if selection.provider is None or selection.provider == provider.name
    ]
    missing = [
        provider.name
        for provider in selected_providers
        if not credentials.get(provider.credential_env, "").strip()
    ]
    report = {
        "experiment": "E1",
        "protocol_version": "1",
        "adapter_revision": "4",
        "session": session,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "not_passed",
        "execution_mode": "http_fixture" if transport is not None else "live",
        "gate_passed": False,
        "diagnostic_only": selection.diagnostic_only,
        "selection": selection.model_dump(),
        "model_calls_definition": "SDK／基础请求开始尝试数；不代表供应商受理或计费次数",
        "configuration_sha256": hashlib.sha256(
            configuration.model_dump_json().encode()
        ).hexdigest(),
        "limits": configuration.limits.model_dump(),
        "versions": {
            name: version(name)
            for name in (
                "deepagents",
                "langgraph",
                "langchain-openai",
                "openai",
            )
        },
        "preflight_errors": [],
        "matrix": [],
        "scope": "P0 E1；不构成产品供应商准入或业务任务恢复授权",
    }
    shared = {"calls": 0, "deadline": time.monotonic() + configuration.limits.total_seconds}
    for provider in configuration.providers:
        for api in APIS:
            for role in ROLES:
                selected = selection.matches(provider, api, role)
                if missing or not selected or not selected_providers:
                    report["matrix"].append(
                        {
                            "provider": provider.name,
                            "api": api,
                            "role": role,
                            "model": getattr(provider, role).model,
                            "status": "not_run",
                            "error": (
                                "selection_not_found"
                                if not selected_providers
                                else "not_selected"
                                if not selected
                                else "credentials_missing"
                            ),
                            "stage": selection.stage,
                            "http_status": None,
                            "failure_detail": None,
                            "usage": ProbeMeter(configuration.limits, shared).summary(),
                            "tools_executed": [],
                        }
                    )
                    continue
                with httpx.Client(
                    transport=transport,
                    timeout=configuration.limits.request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    report["matrix"].append(
                        run_cell(
                            provider,
                            api,
                            role,
                            credentials[provider.credential_env],
                            configuration.limits,
                            shared,
                            client,
                            session,
                            selection.stage,
                        )
                    )
    if missing:
        report["preflight_errors"] = [{"code": "credentials_missing", "providers": missing}]
    if not selected_providers:
        report["preflight_errors"] = [{"code": "selection_not_found"}]
    selected_cells = [cell for cell in report["matrix"] if cell.get("error") != "not_selected"]
    if selected_cells and all(cell["status"] == "passed" for cell in selected_cells):
        report["status"] = "passed"
        report["gate_passed"] = transport is None and not selection.diagnostic_only
    report["model_calls"] = shared["calls"]
    report["finished_at"] = datetime.now(UTC).isoformat()
    return redact(report, [value for value in credentials.values() if value])


def probe_from_file(
    config_path: Path,
    credentials: Mapping[str, str] | None = None,
    *,
    selection: ProbeSelection | None = None,
):
    try:
        if config_path.stat().st_size > 65536:
            raise ValueError("配置过大")
        configuration = ProbeConfiguration.model_validate_json(config_path.read_text("utf-8"))
    except (OSError, ValueError):
        return {
            "experiment": "E1",
            "status": "not_passed",
            "execution_mode": "not_run",
            "gate_passed": False,
            "model_calls": 0,
            "matrix": [
                {
                    "provider": f"unconfigured-{slot}",
                    "api": api,
                    "role": role,
                    "status": "not_run",
                    "error": "configuration_missing_or_invalid",
                    "http_status": None,
                    "failure_detail": None,
                }
                for slot in (1, 2)
                for api in APIS
                for role in ROLES
            ],
            "preflight_errors": [{"code": "configuration_missing_or_invalid"}],
        }
    source = os.environ if credentials is None else credentials
    selected = {
        provider.credential_env: source.get(provider.credential_env, "")
        for provider in configuration.providers
    }
    return run_probe(configuration, selected, selection=selection)
