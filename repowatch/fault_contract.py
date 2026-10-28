"""Small, allowlisted fault envelopes. Never accept exception or provider bodies."""

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_DIAGNOSTIC_BYTES = 32768
STATES = ("pending", "diagnosing", "recovering", "verifying", "resolved", "blocked", "dismissed")
FLOWS = ("analysis", "execution", "diagnosis", "repair", "acceptance", "publication", "service")
SECRET = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_|sk-)[\w-]+|https?://\S+|"
    r"(?:authorization|api[_-]?key|access[_-]?token|password|secret)\s*[:=].*|"
    r"bearer\s+\S+"
)
TOKEN = re.compile(r"[A-Za-z0-9_./:\[\]-]{1,240}\Z")


def token(value, limit=160):
    if not isinstance(value, str) or SECRET.search(value) or not TOKEN.fullmatch(value):
        return None
    return value[:limit]


def note(value):
    return SECRET.sub("[已脱敏]", str(value))[:512].strip()


def number(value):
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12:
        return value
    return None


NUMBERS = frozenset(
    {
        "calls",
        "matching",
        "other",
        "allowed",
        "corrections_remaining",
        "messages",
        "refusals",
        "text_chars",
        "wire_tool_calls",
        "wire_content_chars",
        "wire_content_parts",
        "http_status",
        "request_timeout_seconds",
        "request_elapsed_seconds",
        "attempt_elapsed_seconds",
        "attempt_timeout_seconds",
        "position",
        "line",
        "column",
        "turn",
        "request",
        "max_tokens",
        "output_tokens",
        "reasoning_tokens",
        "payload_tokens",
        "requests",
        "max_requests",
        "resumptions",
        "max_resumptions",
        "remaining_seconds",
        "used",
        "output_used",
        "format_used",
        "evidence_used",
        "required_read_used",
        "tool_arguments_used",
        "attempt",
        "attempt_limit",
        "model_calls",
        "max_model_calls",
        "deadline",
        "retry_after",
        "exit_code",
        "version",
        "model_version",
        "provider_version",
        "configuration_version",
        "request_id",
        "timeouts",
        "outputs",
        "verification_corrections",
        "review_corrections",
        "tool_selection_corrections",
    }
)
FLAGS = frozenset(
    {
        "accepted",
        "recoverable",
        "retryable",
        "truncated",
        "wire_legacy_call",
        "wire_message_present",
        "can_expand",
        "passed",
        "executed",
    }
)
LABELS = frozenset(
    {
        "role",
        "stage",
        "code",
        "reason",
        "recovery",
        "required",
        "expected",
        "actual_type",
        "expected_type",
        "path",
        "field",
        "constraint",
        "tool",
        "type",
        "exception_type",
        "stop_reason",
        "finish_reason",
        "wire_finish_reason",
        "timeout_kind",
        "timing_scope",
        "model",
        "provider_id",
        "reasoning_effort",
        "api",
        "output_protocol",
        "finalization_mode",
        "category",
        "file",
    }
)
NESTED = frozenset(
    {
        "diagnostic",
        "budget",
        "retry_budget",
        "model_binding",
        "errors",
        "calls",
        "output_validation",
        "evidence_validation",
        "locations",
    }
)


def diagnostic(value, depth=0):
    """No arbitrary dictionaries/strings survive, including nested model output."""
    if depth > 4 or not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in ("fields", "issues") and isinstance(item, list):
            result[key] = [safe for part in item[:32] if (safe := token(part)) is not None]
            if len(item) > 32:
                result["truncated"] = True
        elif key in FLAGS and type(item) is bool:
            result[key] = item
        elif key in NUMBERS and number(item) is not None:
            result[key] = number(item)
        elif key in LABELS:
            if key == "path" and isinstance(item, list):
                result[key] = [part if type(part) is int else token(part) for part in item[:12]]
            else:
                result[key] = token(item)
        elif key in NESTED and isinstance(item, dict):
            result[key] = diagnostic(item, depth + 1)
        elif key in NESTED and isinstance(item, list):
            result[key] = [
                diagnostic(part, depth + 1) for part in item[:32] if isinstance(part, dict)
            ]
            if len(item) > 32:
                result["truncated"] = True
    if len(json.dumps(result).encode()) > MAX_DIAGNOSTIC_BYTES:
        return {"truncated": True}
    return result


class FaultEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_event_key: str = Field(min_length=1, max_length=240)
    flow: Literal[
        "analysis", "execution", "diagnosis", "repair", "acceptance", "publication", "service"
    ]
    task_id: str
    root_task_id: str | None = None
    source_table: str
    source_id: str
    occurred: float = Field(ge=0, allow_inf_nan=False)
    repo_id: str | None = None
    account_id: int | None = None
    env_id: str | None = None
    attempt: int | None = None
    role: str | None = None
    stage: str | None = None
    code: str = "unknown"
    category: str = "unknown"
    execution: Literal["not_executed", "executed", "unknown"] = "unknown"
    revision: str | None = None
    workbench_version: str | None = None
    model_binding: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)
    diagnostic: dict = Field(default_factory=dict)
    trace_id: str | None = None
    historical: bool = False

    def safe(self):
        # Revalidate assignments as well as constructor input before allowlisting.
        result = FaultEvent.model_validate(dict(self.__dict__)).model_dump()
        for key, value in result.items():
            if isinstance(value, str):
                result[key] = token(value, 240 if key == "source_event_key" else 160)
        for key in ("model_binding", "budget", "diagnostic"):
            result[key] = diagnostic(result[key])
        result["code"] = result["code"] or "unknown"
        if not result["source_event_key"] or not result["task_id"] or not result["source_id"]:
            raise ValueError("fault_identity_invalid")
        return result


def fingerprint(event):
    # Task identity/time/counts do not decide grouping. Missing bases stay task-isolated.
    basis = [
        event.get(key)
        for key in (
            "account_id",
            "repo_id",
            "env_id",
            "flow",
            "stage",
            "role",
            "code",
            "category",
            "revision",
            "workbench_version",
            "model_binding",
        )
    ]
    detail = event["diagnostic"]
    basis += [detail.get(key) for key in ("reason", "required", "tool", "path", "constraint")]
    if event.get("workbench_version") is None or event.get("revision") is None:
        basis += [event["task_id"]]
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
