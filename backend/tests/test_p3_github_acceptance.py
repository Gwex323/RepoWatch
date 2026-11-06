import importlib.util
import json
from pathlib import Path

import pytest

from repowatch.domain.triage import TriageAnalysis, classify_analysis


@pytest.fixture
def acceptance_script():
    path = Path(__file__).resolve().parents[2] / "experiments" / "p3-github-acceptance.py"
    specification = importlib.util.spec_from_file_location("p3_acceptance", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.ACTIVE = module.DEFECTS["mul"]
    return module


def test_fixture_triage_handles_new_pr_ids_and_waits_for_same_problem(acceptance_script):
    pulls = [
        {"id": 9001, "title": "Fix sub()", "state": "open"},
        {
            "id": 9002,
            "title": "RepoWatch: fix issue #15",
            "state": "open",
            "files": [
                {
                    "filename": "mul.py",
                    "patch": "@@ -1 +1 @@\n-return left + right\n+return left * right",
                }
            ],
        },
        {"id": 9003, "title": "Unclear change", "state": "open"},
        {
            "id": 9004,
            "title": "RepoWatch: fix issue #1",
            "state": "open",
            "files": [
                {
                    "filename": "calc.py",
                    "patch": "@@ -1 +1 @@\n-    return left - right\n+    return left + right",
                }
            ],
        },
    ]
    context = {
        "facts": {"pull_requests": pulls},
        "decisions": {},
        "managed_pr_id": None,
    }
    result = acceptance_script.triage({"objective": "triage"}, {"remote_context": context})
    assert {item["id"]: item["relation"] for item in result["pr_relations"]} == {
        "9001": "unrelated",
        "9002": "same",
        "9003": "uncertain",
        "9004": "unrelated",
    }
    decision = classify_analysis(TriageAnalysis.model_validate(result), remote_context=context)
    assert decision.reason_code == "external_pr_same_problem"


def test_fixture_lookup_reuses_existing_issue_on_repeated_runs(acceptance_script):
    issues = [
        {"number": 5, "state": "closed", "title": "Make mul() return the arithmetic product"},
        {"number": 15, "state": "open", "title": "Make mul() return the arithmetic product"},
    ]
    envelope = {
        "objective": "完整 Issue 查重结果：" + json.dumps({"issues": issues}) + "；后续指令"
    }
    first = acceptance_script.triage(envelope, {})
    second = acceptance_script.triage(envelope, {})
    assert first == second
    assert first["issue_relation"]["relation"] == "same"
    assert first["issue_relation"]["issue_number"] == 15


def test_file_overlap_alone_does_not_claim_pr_is_related(acceptance_script):
    pull = {
        "id": 9010,
        "title": "Update a docstring",
        "files": [{"filename": "mul.py", "patch": "@@ -1 +1 @@\n-Old docs\n+New docs"}],
    }
    assert acceptance_script.fixture_subject(pull) is None
