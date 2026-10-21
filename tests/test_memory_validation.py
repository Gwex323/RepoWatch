import asyncio
import json
import shutil

import pytest

from repowatch.memory_validation import DEFAULT_CASES, load_cases, run_suite
from repowatch.repository_analysis import ROLES


class ValidationEngine:
    def __init__(self, *, fail_memory=False):
        self.contexts = []
        self.fail_memory = fail_memory

    def status(self):
        return {"enabled": True, "model": "fixture", "api": "responses", "version": 1,
                "api_key": "must-not-persist"}

    async def analyze(self, context, event):
        self.contexts.append(context)
        event("model_call", "fixture")
        if self.fail_memory and context["memories"]:
            raise ValueError("analysis_evidence_unverified")
        path, source = next(iter(context["files"].items()))
        findings = []
        clean_repos = {
            "fixture/clamp", "fixture/dedupe_stable", "fixture/config_control",
            "fixture/scope_control",
        }
        if context["memories"] and context["repo_id"] not in clean_repos:
            findings.append({
                "title": "Boundary behavior needs review", "severity": "P2",
                "explanation": "The source may disagree with its documented boundary.",
                "expected": "Documented boundary behavior",
                "suspected_actual": "Different result at the boundary",
                "reproduction": ["Call the function at the documented boundary."],
                "evidence": [{"path": path, "start_line": 1, "end_line": 1,
                              "quote": source.splitlines()[0]}],
            })
        return {
            "summary": "Fixture analysis", "limitations": [], "findings": findings,
            "handoffs": [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])],
            "files_read": [path], "execution_read": True,
            "read_ranges": {path: [[1, len(source.splitlines())]]},
        }


def test_fixed_cases_verify_hashes_and_filter_memory_scope(tmp_path):
    cases = load_cases(DEFAULT_CASES)
    assert {case["id"] for case in cases} == {
        "pagination", "expiry", "clamp", "dedupe_first", "dedupe_stable",
        "config_precedence", "config_control", "scope_boundary", "scope_control",
        "retry_budget", "quota_rejection", "version_numeric",
    }
    assert sum(case["gold"]["known_defect"] for case in cases) == 8
    for case in cases:
        assert len(case["memories"]) == 1
        assert case["memories"][0]["id"].startswith(f"fixture-{case['id']}")

    copy = tmp_path / "fixtures"
    shutil.copytree(DEFAULT_CASES.parent, copy)
    (copy / "pagination/pager.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="memory_validation_source_changed"):
        load_cases(copy / "cases.json")


def test_fixed_suite_persists_paired_results_without_gold_or_credentials(tmp_path):
    engine = ValidationEngine()
    path = asyncio.run(run_suite(DEFAULT_CASES, tmp_path, engine, selected="pagination"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["cases"]["pagination"]
    assert saved["manifest"] == "cases.json"
    assert len(saved["manifest_sha256"]) == 64
    assert saved["summary"]["case_count"] == 1
    assert saved["summary"]["pairs_completed"] == 1
    assert saved["summary"]["known_defect_path_candidates"] == {
        "baseline": 0, "memory": 1,
    }
    assert entry["baseline"]["status"] == "completed"
    assert entry["memory"]["status"] == "completed"
    assert entry["summary"]["finding_counts"] == {"baseline": 0, "memory": 1}
    assert len(entry["score"]["variants"]["memory"]["path_candidates"]) == 1
    assert entry["score"]["interpretation"] == "requires_human_review"
    assert [len(context["memories"]) for context in engine.contexts] == [0, 1]
    assert all("gold" not in context for context in engine.contexts)
    assert "must-not-persist" not in path.read_text(encoding="utf-8")


def test_fixed_suite_keeps_successful_variant_after_other_fails(tmp_path):
    path = asyncio.run(run_suite(
        DEFAULT_CASES, tmp_path, ValidationEngine(fail_memory=True), selected="pagination"
    ))
    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["cases"]["pagination"]
    assert entry["baseline"]["status"] == "completed"
    assert entry["memory"]["error_code"] == "analysis_evidence_unverified"
    assert entry["summary"]["conclusion"] == "insufficient_evidence"
    assert entry["score"]["interpretation"] == "insufficient_evidence"
    assert saved["summary"]["pairs_incomplete"] == 1
    assert saved["summary"]["model_calls"] == {"baseline": 1, "memory": 1}


def test_fixed_suite_runs_known_and_clean_cases(tmp_path):
    path = asyncio.run(run_suite(DEFAULT_CASES, tmp_path, ValidationEngine()))
    saved = json.loads(path.read_text(encoding="utf-8"))
    cases = saved["cases"]
    assert len(cases) == 12
    assert {"pagination", "expiry", "clamp"} <= set(cases)
    assert cases["clamp"]["score"]["variants"]["memory"]["other_findings"] == []
    assert cases["expiry"]["summary"]["finding_counts"]["memory"] == 1
    assert saved["summary"]["case_count"] == 12
    assert saved["summary"]["pairs_completed"] == 12
    assert saved["summary"]["clean_control_findings"] == {"baseline": 0, "memory": 0}


def test_fixed_suite_does_not_compare_different_model_settings(tmp_path):
    class ChangingEngine(ValidationEngine):
        def status(self):
            status = super().status()
            if self.contexts:
                status["model"] = "changed"
            return status

    engine = ChangingEngine()
    path = asyncio.run(run_suite(DEFAULT_CASES, tmp_path, engine, selected="pagination"))
    entry = json.loads(path.read_text(encoding="utf-8"))["cases"]["pagination"]
    assert len(engine.contexts) == 1
    assert entry["baseline"]["status"] == "completed"
    assert entry["memory"]["error_code"] == "analysis_model_changed"
    assert entry["summary"]["conclusion"] == "insufficient_evidence"


def test_fixed_suite_rejects_reported_model_mismatch(tmp_path):
    class ChangingReportEngine(ValidationEngine):
        async def analyze(self, context, event):
            report = await super().analyze(context, event)
            report["model_version"] = "memory" if context["memories"] else "baseline"
            return report

    path = asyncio.run(run_suite(
        DEFAULT_CASES, tmp_path, ChangingReportEngine(), selected="pagination"
    ))
    entry = json.loads(path.read_text(encoding="utf-8"))["cases"]["pagination"]
    assert entry["summary"]["model_consistent"] is False
    assert entry["summary"]["conclusion"] == "insufficient_evidence"
    assert entry["score"]["interpretation"] == "insufficient_evidence"


def test_fixed_suite_rejects_one_sided_model_metadata(tmp_path):
    class PartialReportEngine(ValidationEngine):
        async def analyze(self, context, event):
            report = await super().analyze(context, event)
            if context["memories"]:
                report["model_version"] = "memory-only"
            return report

    path = asyncio.run(run_suite(
        DEFAULT_CASES, tmp_path, PartialReportEngine(), selected="pagination"
    ))
    entry = json.loads(path.read_text(encoding="utf-8"))["cases"]["pagination"]
    assert entry["summary"]["model_consistent"] is False
    assert entry["score"]["interpretation"] == "insufficient_evidence"
