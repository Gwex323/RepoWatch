import pytest

from repowatch.domain.verification import (
    ProbePlan,
    compare_regression,
    probe_verdict,
    weakening_paths,
)


def result(exit_code=0):
    return {"exit_code": exit_code, "argv": ["pytest"], "image": "sha256:fixture"}


def probe():
    return ProbePlan(
        argv=["python", "probe.py"],
        target_failure="TARGET_BUG",
        target_success="TARGET_OK",
        purpose="sum regression",
    )


@pytest.mark.parametrize(
    "log",
    [
        "ModuleNotFoundError: missing",
        "ERROR collecting tests",
        "SyntaxError: invalid syntax",
        "Permission denied",
    ],
)
def test_environment_errors_are_never_valid_reproduction(log):
    assert probe_verdict(result(1), "TARGET_BUG\n" + log, probe(), side="base") == "unable"


def test_probe_requires_actual_target_failure_then_success():
    assert probe_verdict(result(1), "TARGET_BUG", probe(), side="base") == "target_failed"
    assert probe_verdict(result(1), "different failure", probe(), side="base") == "not_reproduced"
    assert probe_verdict(result(0), "TARGET_OK", probe(), side="candidate") == "passed"
    assert probe_verdict(result(0), "", probe(), side="candidate") == "failed"


def test_same_failure_count_with_different_cases_is_rejected():
    verdict = compare_regression(
        result(1), result(1), "FAILED tests/a.py::old\n1 failed", "FAILED tests/a.py::new\n1 failed"
    )
    assert not verdict["passed"] and verdict["reason"] == "new_failure_identity"


def test_new_skips_and_reduced_discovery_are_rejected():
    assert (
        compare_regression(result(), result(), "10 passed", "9 passed")["reason"]
        == "test_discovery_reduced"
    )
    assert (
        compare_regression(result(), result(), "10 passed", "9 passed, 1 skipped")["reason"]
        == "new_skips"
    )


def test_existing_failures_require_independent_reason_and_matching_identity():
    log = "FAILED tests/a.py::old - known\n9 passed, 1 failed"
    assert not compare_regression(result(1), result(1), log, log)["passed"]
    assert compare_regression(
        result(1), result(1), log, log, unrelated_failures=["tests/a.py::old - known"]
    )["passed"]


def test_assertion_deletion_and_expanded_skip_are_rejected():
    assert weakening_paths([("tests/a.py", b"assert value == 1", b"pass")]) == ["tests/a.py"]
    assert weakening_paths([("pytest.ini", b"", b"ignore = tests")]) == ["pytest.ini"]


def test_same_failure_identity_with_different_cause_is_rejected():
    summary = "FAILED tests/a.py::old - known\n9 passed, 1 failed"
    verdict = compare_regression(
        result(1),
        result(1),
        "E AssertionError: old issue\n" + summary,
        "E TypeError: new issue\n" + summary,
        unrelated_failures=["tests/a.py::old - known"],
    )
    assert verdict == {"passed": False, "reason": "existing_failure_cause_changed"}
