"""宿主核验目标证据、基线差异及冻结候选的一致性。"""

import re
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field


class ProbePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str] = Field(default_factory=dict, max_length=100)
    argv: list[str] = Field(min_length=1, max_length=100)
    target_failure: str = Field(min_length=1, max_length=1000)
    target_success: str = Field(min_length=1, max_length=1000)
    timeout: int = Field(default=60, ge=1, le=3600, strict=True)
    purpose: str = Field(min_length=1, max_length=4000)
    behavior_argv: list[str] = Field(default_factory=list, max_length=100)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool = Field(strict=True)
    probe_valid: bool = Field(strict=True)
    weakened_acceptance: bool = Field(strict=True)
    findings: list[str] = Field(default_factory=list, max_length=100)
    unrelated_failures: list[str] = Field(default_factory=list, max_length=100)
    rationale: str = Field(min_length=1, max_length=12000)


ENVIRONMENT_FAILURE = re.compile(
    r"ModuleNotFoundError|ImportError|command not found|No module named|not found:|"
    r"ERROR collecting|REPOWATCH_ENVIRONMENT_UNAVAILABLE|Permission denied|"
    r"Cannot find module|npm ERR!|SyntaxError|IndentationError",
    re.IGNORECASE,
)


def executed(result, log):
    return bool(
        type(result.get("exit_code")) is int
        and not any(
            result.get(key)
            for key in (
                "timed_out",
                "control_stopped",
                "oom_killed",
                "log_truncated",
                "preparation_report",
            )
        )
        and not ENVIRONMENT_FAILURE.search(log)
    )


def probe_verdict(result, log, plan, *, side):
    if not executed(result, log):
        return "unable"
    if side == "base":
        return (
            "target_failed"
            if result["exit_code"] != 0 and plan.target_failure in log
            else "not_reproduced"
        )
    return "passed" if result["exit_code"] == 0 and plan.target_success in log else "failed"


def test_summary(log):
    failures = sorted(set(re.findall(r"^(?:FAILED|ERROR)\s+([^\n]+)", log, re.MULTILINE)))
    totals = Counter()
    for count, status in re.findall(
        r"(\d+) (passed|failed|skipped|xfailed|xpassed|error[s]?|deselected)", log
    ):
        totals[status.rstrip("s") if status.startswith("error") else status] = int(count)
    discovered = sum(totals.values())
    return {"failures": failures, "counts": dict(totals), "discovered": discovered}


def compare_regression(base, candidate, base_log, candidate_log, *, unrelated_failures=()):
    if not executed(base, base_log) or not executed(candidate, candidate_log):
        return {"passed": False, "reason": "check_unavailable"}
    if base["argv"] != candidate["argv"] or base["image"] != candidate["image"]:
        return {"passed": False, "reason": "incomparable_environment_or_command"}
    before, after = test_summary(base_log), test_summary(candidate_log)
    if after["discovered"] < before["discovered"]:
        return {"passed": False, "reason": "test_discovery_reduced"}
    if after["counts"].get("skipped", 0) > before["counts"].get("skipped", 0):
        return {"passed": False, "reason": "new_skips"}
    if set(after["failures"]) - set(before["failures"]):
        return {"passed": False, "reason": "new_failure_identity"}
    if candidate["exit_code"] != 0:
        if not after["failures"] or not set(after["failures"]) <= set(unrelated_failures):
            return {"passed": False, "reason": "unexplained_existing_failure"}
        before_causes = re.findall(r"^(?:E\s+.*|.*(?:Error|Exception):.*)$", base_log, re.MULTILINE)
        after_causes = re.findall(
            r"^(?:E\s+.*|.*(?:Error|Exception):.*)$", candidate_log, re.MULTILINE
        )
        if sorted(after_causes) != sorted(before_causes):
            return {"passed": False, "reason": "existing_failure_cause_changed"}
    return {"passed": True, "reason": "baseline_not_worsened", "base": before, "candidate": after}


def weakening_paths(changes):
    paths = []
    for path, before, after in changes:
        old, new = before.decode("utf-8", "replace"), after.decode("utf-8", "replace")
        if (
            old.count("assert ") > new.count("assert ")
            or new.count("skip") > old.count("skip")
            or new.count("ignore") > old.count("ignore")
            or (
                re.search(r"(?:test|spec|pytest|jest|vitest|coverage)", path, re.I)
                and before
                and not after
            )
        ):
            paths.append(path)
    return paths
