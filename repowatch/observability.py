"""Optional, metadata-only Langfuse instrumentation for the real model flows.

No SDK import or exporter exists until explicitly enabled. Observations use
explicit parents: SDK hooks may execute in different asyncio tasks, so keeping
an OpenTelemetry context token open across hook callbacks would be incorrect.
"""

import asyncio
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit
from uuid import uuid4

from agents import RunHooks

_binding = ContextVar("repowatch_trace_binding", default=None)
_run = ContextVar("repowatch_trace_run", default=None)
_IDENTIFIERS = {"task_id", "repo_id", "env_id", "revision", "attempt", "issue_number"}
_EVENTS = {
    "tool_arguments_invalid", "tool_arguments_retry", "tool_arguments_repaired",
    "tool_output_budget", "tool_output_retry", "tool_access_rejected", "scope_registered",
    "timeout_retry", "retry_call", "output_retry", "format_retry", "evidence_retry",
    "handoff_retry", "report_finalization", "report_validation", "verification_resume",
}
_FAILURE_CODES = {
    "analysis_provider_timeout_exhausted", "analysis_deadline_exceeded", "analysis_cancelled",
    "analysis_service_stopped", "analysis_model_disabled", "analysis_model_required",
    "analysis_model_changed", "analysis_output_truncated", "analysis_json_invalid",
    "analysis_report_invalid", "analysis_evidence_unverified", "analysis_handoff_not_followed",
    "analysis_tool_arguments_exhausted", "analysis_tool_not_allowed",
    "analysis_scope_unregistered", "analysis_scope_read_not_followed",
    "repair_deadline_exceeded", "repair_tests_failed", "repair_verification_required",
    "repair_output_truncated", "repair_handoff_incomplete",
}


@contextmanager
def task_binding(**identifiers):
    """Pass correlation IDs to a created task without changing its model input."""
    safe = {key: value for key, value in identifiers.items()
            if key in _IDENTIFIERS and (
                type(value) is int or isinstance(value, str)
                and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", value)
            )}
    token = _binding.set(safe)
    try:
        yield
    finally:
        _binding.reset(token)


class Observation:
    def __init__(self, owner, span=None):
        self.owner, self.span = owner, span

    def update(self, **values):
        if self.span is not None:
            try:
                self.span.update(**values)
            except Exception:
                self.owner.failed()

    def end(self, error=None, *, outcome="completed"):
        if self.span is None:
            return
        if error is not None:
            cancelled = isinstance(error, asyncio.CancelledError)
            details = {"outcome": "cancelled" if cancelled else "failed",
                       "error_type": type(error).__name__}
            # Only known local codes and numeric status codes; provider exception
            # strings can contain request bodies, headers or credential echoes.
            try:
                if type(error).__name__ in {"AnalysisFailure", "Conflict"}:
                    code = str(error)
                    if code in _FAILURE_CODES:
                        details["error_code"] = code
                status = getattr(error, "status_code", None)
                if type(status) is int and 100 <= status <= 599:
                    details["http_status"] = status
            except Exception:
                self.owner.failed()
            self.update(
                level="WARNING" if cancelled else "ERROR",
                status_message="cancelled" if cancelled else "failed",
                metadata=details,
            )
        else:
            self.update(metadata={"outcome": outcome})
        try:
            self.span.end()
        except Exception:
            self.owner.failed()
        self.span = None


class TraceRun:
    def __init__(self, owner, flow, context):
        self.owner, self.agent, self.tools, self.calls = owner, None, {}, 0
        self.role, self.models = None, {}
        self.trace_id = uuid4().hex
        metadata = {**(_binding.get() or {}), "flow": flow, "capture_mode": "metadata_only",
                    "scope": "model_flow"}
        issue = context.get("issue") or context.get("source_issue") or {}
        if isinstance(issue, dict) and type(issue.get("number")) is int:
            metadata["issue_number"] = issue["number"]
        revision = context.get("revision")
        if (not metadata.get("revision") and isinstance(revision, str)
                and re.fullmatch(r"[a-fA-F0-9]{7,64}", revision)):
            metadata["revision"] = revision
        self.root = self.start(f"repowatch.{flow}", "chain", metadata=metadata)

    def start(self, name, kind, *, parent=None, **values):
        parent_span = parent.span if parent is not None else None
        try:
            span = self.owner.client.start_observation(
                name=name, as_type=kind,
                trace_context={"trace_id": self.trace_id,
                               **({"parent_span_id": parent_span.id} if parent_span else {})},
                **values,
            )
            return Observation(self.owner, span)
        except Exception:
            self.owner.failed()
            return Observation(self.owner)

    def agent_start(self, name):
        if self.agent:
            self.agent.end(outcome="handoff")
        self.agent = self.start(name, "agent", parent=self.root)
        self.role = name

    def event(self, kind, value):
        if kind == "model_configuration":
            try:
                data = json.loads(value)
                self.models[data["role"]] = {
                    key: data[key] for key in ("model", "provider_id") if key in data
                }
            except (TypeError, ValueError, KeyError):
                self.owner.failed()
        if kind in _EVENTS:
            self.start(kind, "span", parent=self.agent or self.root,
                       metadata={"request_index": self.calls}).end()

    def close(self, error=None):
        for tool in self.tools.values():
            tool.end(error, outcome="unfinished")
        self.tools.clear()
        if self.agent:
            self.agent.end(error)
        self.root.end(error)


class Telemetry:
    def __init__(self):
        self.client = None
        self.provider = None
        self.base_url = None
        self.reason = "disabled"
        self._warned = False

    @classmethod
    def from_environment(cls):
        instance = cls()
        if os.getenv("REPOWATCH_LANGFUSE_ENABLED") != "1":
            return instance
        if os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in {"false", "0"}:
            return instance
        base_url = os.getenv("LANGFUSE_BASE_URL", "").strip().rstrip("/")
        public = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        secret = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
        if not all((base_url, public, secret)):
            instance.reason = "configuration_required"
            return instance
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("invalid destination")
            parsed.port
        except ValueError:
            instance.reason = "invalid_url"
            return instance
        instance.base_url = base_url
        try:
            from langfuse import Langfuse
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            instance.reason = "sdk_required"
            return instance
        except Exception:
            instance.reason = "initialization_failed"
            instance.failed()
            return instance
        try:
            # Keep exporters isolated from other instrumented libraries and never
            # register the Agents SDK's default OpenAI trace processor.
            instance.provider = TracerProvider(
                resource=Resource.create({"service.name": "repowatch"}), shutdown_on_exit=False,
            )
            instance.client = Langfuse(
                public_key=public, secret_key=secret, base_url=base_url,
                timeout=3, debug=False, tracing_enabled=True, sample_rate=1.0,
                tracer_provider=instance.provider,
            )
            instance.reason = "initialized"
        except Exception:
            instance.reason = "initialization_failed"
            instance.failed()
            instance.shutdown()
        return instance

    def failed(self):
        if not self._warned:
            logging.getLogger(__name__).warning(
                "Langfuse instrumentation unavailable; repository work will continue."
            )
            self._warned = True

    def status(self):
        return {"enabled": self.client is not None, "reason": self.reason,
                "base_url": self.base_url, "capture_mode": "metadata_only",
                "connection_verified": False, "instrumentation_error": self._warned}

    @contextmanager
    def workflow(self, flow, context, event):
        state = None
        if self.client is not None:
            try:
                state = TraceRun(self, flow, context)
                if state.root.span is not None:
                    event("langfuse_trace", state.trace_id)
            except Exception:
                self.failed()
        token = _run.set(state)
        try:
            yield state
        except BaseException as error:
            if state:
                state.close(error)
            raise
        else:
            if state:
                state.close()
        finally:
            _run.reset(token)

    def shutdown(self):
        if self.client is not None:
            try:
                self.client.shutdown()
            except Exception:
                self.failed()
        if self.provider is not None:
            try:
                self.provider.shutdown()
            except Exception:
                self.failed()


def record_event(kind, value):
    state = _run.get()
    if state:
        state.event(kind, value)


@contextmanager
def model_observation(config, settings=None):
    state = _run.get()
    observation = Observation(state.owner if state else None)
    if state:
        state.calls += 1
        config = {**config, **state.models.get(state.role, {})}
        metadata = {key: config[key] for key in ("provider_id", "api") if key in config}
        metadata["request_index"] = state.calls
        metadata["role"] = state.role
        parameters = {}
        if settings is not None:
            if type(settings.max_tokens) is int:
                parameters["max_tokens"] = settings.max_tokens
            effort = getattr(settings.reasoning, "effort", None)
            if effort in {"low", "medium", "high", "max", "minimal", "none", "xhigh"}:
                parameters["reasoning_effort"] = effort
        observation = state.start(
            "model.request", "generation", parent=state.agent or state.root,
            model=config.get("model"), metadata=metadata, model_parameters=parameters,
        )
    try:
        yield observation
    except BaseException as error:
        observation.end(error)
        raise
    else:
        observation.end()


def record_usage(observation, response):
    if observation.span is None:
        return
    try:
        usage = getattr(response, "usage", None)
        details = {name: value for name, field in (("input", "input_tokens"),
                                                  ("output", "output_tokens"))
                   if type(value := getattr(usage, field, None)) is int and value >= 0}
        if details:
            observation.update(usage_details=details)
    except Exception:
        observation.owner.failed()


class TracingHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        if state := _run.get():
            state.agent_start(agent.name)

    async def on_agent_end(self, context, agent, output):
        if (state := _run.get()) and state.agent:
            state.agent.end(outcome="output_produced")
            state.agent = None
            state.role = None

    async def on_handoff(self, context, from_agent, to_agent):
        if state := _run.get():
            state.start("agent.handoff", "span", parent=state.agent or state.root,
                        metadata={"from_role": from_agent.name, "to_role": to_agent.name}).end()
            if state.agent:
                state.agent.end(outcome="handoff")
                state.agent = None
                state.role = None

    async def on_tool_start(self, context, agent, tool):
        if state := _run.get():
            state.tools[id(context)] = state.start(
                tool.name, "tool", parent=state.agent or state.root,
                metadata={"role": agent.name},
            )

    async def on_tool_end(self, context, agent, tool, result):
        if (state := _run.get()) and (span := state.tools.pop(id(context), None)):
            span.end()
