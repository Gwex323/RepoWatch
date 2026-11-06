import json

import pytest

from repowatch.domain.triage import (
    TriageAnalysis,
    admission_eligible,
    classify_analysis,
    host_triage_gate,
)


def repository(mode="all_bugs", **policy):
    return {
        "admission_mode": mode,
        "admission_revision": 1,
        "policy_json": json.dumps(policy),
        "identity_json": json.dumps({"execution_login": "worker"}),
    }


def task(**changes):
    return {
        "input_revision": 1,
        "selected": 0,
        "user_excluded": 0,
        "cancel_requested": 0,
        **changes,
    }


def analysis(**changes):
    return TriageAnalysis.model_validate(
        {
            "kind": "bug",
            "objective": "return correct sum",
            "criteria": ["sum(2, 3) == 5"],
            "reproduction_steps": ["call sum(2, 3)"],
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("mode", "labels", "allowed", "selected", "expected"),
    [
        ("manual", ["bug"], ["bug"], False, False),
        ("manual", [], [], True, True),
        ("labels", ["bug"], [], False, False),
        ("labels", ["bug"], ["bug"], False, True),
        ("labels", [], ["bug"], False, False),
        ("labels", [], [], True, True),
        ("all_bugs", [], [], False, True),
    ],
)
def test_admission_modes_do_not_require_bug_label_in_all_bugs(
    mode, labels, allowed, selected, expected
):
    assert (
        admission_eligible(repository(mode, allowed_labels=allowed), {"labels": labels}, selected)
        is expected
    )


@pytest.mark.parametrize(
    ("issue", "changes", "expected"),
    [
        ({"assignees": ["other"]}, {"selected": 1}, "other_assignee"),
        ({"labels": ["wontfix"]}, {"selected": 1}, "excluded_labels"),
        ({"labels": ["needs-info"]}, {}, "needs_information_label"),
        ({"state": "closed"}, {}, "issue_closed"),
        ({}, {"user_excluded": 1}, "user_excluded"),
    ],
)
def test_selection_cannot_override_ownership_exclusion_or_information(issue, changes, expected):
    decision = host_triage_gate(
        repository(excluded_labels=["wontfix", "needs-info"]), task(**changes), issue
    )
    assert decision.reason_code == expected
    assert decision.result != "blocked"


@pytest.mark.parametrize(
    ("changes", "result", "reason"),
    [
        ({}, "auto_repair", "verifiable_bug"),
        ({"sensitive": True}, "needs_information", "sensitive_disclosure"),
        ({"contradictions": ["labels disagree"]}, "needs_information", "contradictory_input"),
        ({"missing_information": ["environment"]}, "needs_information", "information_required"),
        ({"reproduction_steps": []}, "needs_information", "reproduction_path_required"),
        ({"kind": "feature", "plan": "add feature"}, "needs_approval", "plan_approval_required"),
        ({"kind": "refactor", "plan": "reorganize"}, "needs_approval", "plan_approval_required"),
        ({"independent_goals": ["one", "two"]}, "needs_approval", "scope_confirmation"),
    ],
)
def test_analysis_separates_information_approval_and_verifiable_bug(changes, result, reason):
    decision = classify_analysis(analysis(**changes))
    assert (decision.result, decision.reason_code) == (result, reason)


def test_bug_cannot_use_refactor_approval_to_bypass_reproduction():
    decision = classify_analysis(
        analysis(), acceptance={"approved": True, "current": True, "kind": "refactor"}
    )
    assert decision.reason_code == "bug_scope_mismatch"


def test_unapproved_feature_does_not_enter_repair():
    pending = {"approved": False, "kind": "feature"}
    approved = {
        "approved": True,
        "kind": "feature",
        "content": {"objective": "return correct sum", "criteria": ["sum(2, 3) == 5"], "plan": ""},
    }
    assert (
        classify_analysis(analysis(kind="feature"), acceptance=pending).result == "needs_approval"
    )
    assert classify_analysis(analysis(kind="feature"), acceptance=approved).result == "auto_repair"


def test_changed_input_can_propose_a_new_bug_specification():
    acceptance = {"current": False, "approved": False, "kind": "bug", "content": {}}
    assert classify_analysis(analysis(), acceptance=acceptance).result == "auto_repair"


def test_multiple_independent_goals_require_convergence_even_when_spec_approved():
    current = {
        "current": True,
        "approved": True,
        "kind": "bug",
        "content": {
            "objective": "return correct sum",
            "criteria": ["sum(2, 3) == 5"],
            "independent_goals": ["one", "two"],
        },
    }
    assert (
        classify_analysis(analysis(independent_goals=["one", "two"]), acceptance=current).result
        == "needs_approval"
    )
    assert (
        classify_analysis(
            analysis(independent_goals=["one", "three"]), acceptance=current
        ).reason_code
        == "scope_confirmation"
    )
