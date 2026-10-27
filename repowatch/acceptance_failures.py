"""Classify acceptance failures without persisting provider prose or credentials."""

from pathlib import Path
from traceback import walk_tb

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, ModelRefusalError, UserError
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from .analysis_model import OutputTruncated
from .repository_source import ExecutionError
from .store import Conflict

LOCAL_CODES = {
    "acceptance_basis_changed", "acceptance_budget_exhausted", "acceptance_context_limit",
    "acceptance_coverage_required", "acceptance_deadline_exceeded", "acceptance_evidence_invalid",
    "acceptance_model_budget", "acceptance_model_changed", "acceptance_output_invalid",
    "acceptance_output_truncated", "acceptance_paused", "acceptance_runner_unavailable",
    "acceptance_scope_invalid", "acceptance_scope_rejected", "acceptance_source_changed",
    "acceptance_test_invalid", "docker_unavailable",
}
TRANSIENT_CODES = {
    "acceptance_provider_timeout", "acceptance_provider_unreachable", "acceptance_rate_limited",
    "acceptance_provider_unavailable",
}


def exception_chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def failure_code(exc):
    chain = list(exception_chain(exc))
    for error in chain:
        if isinstance(error, (Conflict, ExecutionError)) and str(error) in LOCAL_CODES:
            return str(error)
    for error in chain:
        for kind, code in (
            (OutputTruncated, "acceptance_output_truncated"),
            (APITimeoutError, "acceptance_provider_timeout"),
            (TimeoutError, "acceptance_provider_timeout"),
            (APIConnectionError, "acceptance_provider_unreachable"),
            (MaxTurnsExceeded, "acceptance_model_budget"),
            (ModelRefusalError, "acceptance_model_refused"),
            (ValidationError, "acceptance_output_invalid"),
            (ModelBehaviorError, "acceptance_output_invalid"),
            (UserError, "acceptance_configuration_error"),
        ):
            if isinstance(error, kind):
                return code
        if isinstance(error, APIStatusError):
            status = error.status_code
            if status in (408, 409) or 500 <= status <= 599:
                return "acceptance_provider_unavailable"
            return {
                401: "acceptance_authentication_failed", 403: "acceptance_provider_forbidden",
                404: "acceptance_provider_not_found", 429: "acceptance_rate_limited",
                400: "acceptance_provider_request_rejected",
                422: "acceptance_provider_request_rejected",
            }.get(status, "acceptance_provider_error")
    return "acceptance_execution_failed"


def failure_details(exc):
    package = Path(__file__).resolve().parent
    known = (OutputTruncated, Conflict, ExecutionError, APIStatusError, APITimeoutError,
             APIConnectionError, TimeoutError, ValidationError, ModelBehaviorError, UserError,
             OSError, ValueError, TypeError, KeyError, RuntimeError)
    kinds, locations, statuses = set(), [], set()
    for error in exception_chain(exc):
        kinds.add(next((kind.__name__ for kind in known if isinstance(error, kind)),
                       "unexpected_exception"))
        if isinstance(error, APIStatusError) and type(error.status_code) is int:
            if 100 <= error.status_code <= 599:
                statuses.add(error.status_code)
        for frame, line in walk_tb(error.__traceback__):
            path = Path(frame.f_code.co_filename).resolve()
            if path.is_relative_to(package):
                location = {"file": "repowatch/" + path.relative_to(package).as_posix(),
                            "line": line}
                if location not in locations and len(locations) < 8:
                    locations.append(location)
    return {"exceptions": sorted(kinds), "locations": locations, "http_statuses": sorted(statuses)}
