"""Run a bounded, offline acceptance selection and retain machine-readable evidence."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "test_central_acceptance.py::test_fault_to_reviewed_release_to_original_verification",
    "test_analysis_auto_recovery.py::test_valid_checkpoint_is_automatically_resumed_without_manual_retry",
    "test_workflow_recovery.py::test_two_connections_compete_in_business_transaction",
    "test_workflow_recovery.py::test_crashed_repair_settles_unknown_receipt_then_resumes",
    "test_coordination.py::test_model_outage_without_backup_is_bounded_and_local_ledger_survives",
    "test_analysis_handoff_recovery.py::test_cancel_after_early_stop_cannot_publish_late_recovered_report",
    "test_workflow_recovery.py::test_queued_automatic_retry_rechecks_immutable_basis[model]",
    "test_analysis_report_recovery.py::test_new_execution_cannot_resume_stale_review",
    "test_coordination.py::test_in_flight_diagnosis_loses_authority_and_records_usage[disabled]",
    "test_coordination.py::test_source_gate_stops_before_model_invocation[budget]",
    "test_acceptance_recovery.py::test_acceptance_recovers_truncation_once_on_the_actual_sdk_wire[False]",
    "test_system_faults.py::test_real_acceptance_check_failure_is_target_behavior_and_not_a_system_code_defect",
    "test_repair_review_feedback.py::test_test_failures_and_review_rejections_share_correction_limits",
    "test_releases.py::test_rollback_keeps_new_writes_and_interrupted_release_can_reconcile",
    "test_releases.py::test_release_resumes_only_real_original_adapter_with_budget_and_authorization",
    "test_system_faults.py::test_repair_verified_retry_only_resolves_compatible_prior_attempt[True]",
)


def main():
    output = ROOT / "data" / "central-acceptance" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    xml_path = output / "pytest.xml"
    command = [sys.executable, "-m", "pytest", "-q", "-o", "junit_family=legacy",
               "--junitxml=" + str(xml_path), *("tests/" + case for case in CASES)]
    result = subprocess.run(command, cwd=ROOT, check=False)
    tests = []
    if xml_path.is_file():
        for case in ET.parse(xml_path).iter("testcase"):
            state = next((name for name in ("failure", "error", "skipped")
                          if case.find(name) is not None), "passed")
            properties = {prop.attrib["name"]: json.loads(prop.attrib["value"])
                          for prop in case.findall("properties/property")
                          if prop.attrib["name"] in {"acceptance_trace", "acceptance_metrics"}}
            tests.append({"case": case.attrib["classname"] + "::" + case.attrib["name"],
                          "result": state, "seconds": float(case.attrib["time"]), **properties})
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, check=True,
                           capture_output=True, text=True).stdout.strip()
    passed = result.returncode == 0 and len(tests) == len(CASES) and all(
        test["result"] == "passed" for test in tests)
    report = {
        "format": 1, "issue": 83, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": commit, "worktree_dirty": bool(dirty),
        "scope": "isolated_offline_acceptance", "live_model_calls": 0,
        "live_github_writes": 0, "production_deployed": False,
        "native_process_drill_rerun": False, "full_regression": False,
        "expected": len(CASES), "passed": sum(test["result"] == "passed" for test in tests),
        "result": "passed" if passed else "failed", "exit_code": result.returncode,
        "tests": tests,
    }
    path = output / "result.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0 if passed else (result.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
