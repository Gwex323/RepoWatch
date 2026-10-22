"""Run fixed memory A/B cases against the locally configured analysis model."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from repowatch.analysis_model import AnalysisModel
from repowatch.diagnosis import redact_values
from repowatch.github_accounts import GitHubAccounts
from repowatch.memory import Memory
from repowatch.memory_evaluation import _summary, model_identity, run_variant

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "tests/fixtures/memory_evaluation/cases.json"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data/repowatch.db"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/memory-validation"


class ReadOnlyStore:
    """Give AnalysisModel its existing credential path without modifying the owner database."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def load_cases(manifest_path):
    """Verify every fixed source byte before any model call."""
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("cases"), list):
        raise ValueError("memory_validation_manifest_invalid")
    if not isinstance(manifest.get("memories"), list):
        raise ValueError("memory_validation_manifest_invalid")
    cases, seen = [], set()
    for case in manifest["cases"]:
        case_id = case["id"]
        if case_id in seen or not case["files"] or not case["repo_id"]:
            raise ValueError("memory_validation_case_invalid")
        seen.add(case_id)
        files = {}
        for source_path, relative in case["files"].items():
            source_file = root / relative
            path = source_file.resolve()
            if (source_file.is_symlink() or not path.is_relative_to(root)
                    or not path.is_file()):
                raise ValueError("memory_validation_source_invalid")
            source_bytes = path.read_bytes()
            digest = hashlib.sha256(source_bytes).hexdigest()
            if digest != case["file_sha256"].get(source_path):
                raise ValueError("memory_validation_source_changed")
            files[source_path] = source_bytes.decode("utf-8")
        revision = hashlib.sha256(
            json.dumps(files, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if revision != case["revision"]:
            raise ValueError("memory_validation_revision_changed")
        context = {
            "repo_id": case["repo_id"], "revision": revision, "files": files,
            "config": {"evaluation_fixture": case_id}, "jobs": [], "test_result": None,
            "access_result": None,
        }
        candidates = [
            {**item, "version": 1, "updated": 0, "evidence": "{}"}
            for item in manifest["memories"]
            if item.get("repo_id") == case["repo_id"]
            and item.get("revision") == revision
        ]
        ranked = Memory.rank(candidates, context)[:6]
        memories = [redact_values({
            key: row[key] for key in ("id", "version", "title", "category", "content")
        }) for row in ranked]
        cases.append({"id": case_id, "context": context, "memories": memories,
                      "gold": case["gold"], "file_sha256": case["file_sha256"]})
    return cases


def _suite_summary(cases):
    """Aggregate screening counts without promoting path matches to true positives."""
    summary = {
        "case_count": len(cases), "pairs_completed": 0, "pairs_incomplete": 0,
        "known_defect_path_candidates": {"baseline": 0, "memory": 0},
        "clean_control_findings": {"baseline": 0, "memory": 0},
        "model_calls": {"baseline": 0, "memory": 0},
        "elapsed_seconds": {"baseline": 0, "memory": 0},
        "interpretation": "insufficient_evidence",
    }
    for entry in cases.values():
        if entry["summary"] and entry["summary"]["conclusion"] == "requires_human_review":
            summary["pairs_completed"] += 1
        else:
            summary["pairs_incomplete"] += 1
        for name in ("baseline", "memory"):
            variant = entry[name]
            if variant:
                summary["model_calls"][name] += variant["model_calls"]
                summary["elapsed_seconds"][name] += variant["elapsed_seconds"]
            if entry["score"] and entry["score"]["variants"][name]:
                scored = entry["score"]["variants"][name]
                if entry["score"]["known_defect"]:
                    summary["known_defect_path_candidates"][name] += len(
                        scored["path_candidates"]
                    )
                else:
                    summary["clean_control_findings"][name] += len(
                        scored["other_findings"]
                    )
    if cases and summary["pairs_completed"] == len(cases):
        summary["interpretation"] = "requires_human_review"
    return summary


def _write_result(path, result):
    """Replace one run's result atomically; never overwrite a different run."""
    result["summary"] = _suite_summary(result["cases"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".memory-validation-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _score(gold, baseline, memory, summary):
    """Flag path candidates for human review; a path match alone is not a true positive."""
    expected = set(gold["paths"])
    counts = {}
    for name, variant in (("baseline", baseline), ("memory", memory)):
        if not variant or variant["status"] != "completed":
            counts[name] = None
            continue
        findings = variant["report"].get("findings", [])
        candidates = []
        other = []
        for finding in findings:
            paths = {item["path"] for item in finding.get("evidence", [])}
            item = {"title": finding["title"], "paths": sorted(paths)}
            (candidates if expected and paths & expected else other).append(item)
        counts[name] = {"path_candidates": candidates, "other_findings": other}
    return {
        "known_defect": gold["known_defect"], "expected_behavior": gold["description"],
        "expected_paths": gold["paths"], "variants": counts,
        "interpretation": (
            "requires_human_review" if summary["model_consistent"] is True and all(
                variant and variant["status"] == "completed"
                for variant in (baseline, memory)
            ) else "insufficient_evidence"
        ),
    }


async def run_suite(manifest_path, output_dir, engine, *, selected=None):
    manifest_path = Path(manifest_path).resolve()
    cases = load_cases(manifest_path)
    if selected:
        cases = [case for case in cases if case["id"] == selected]
        if not cases:
            raise ValueError("memory_validation_case_unknown")
    status = engine.status()
    if not status.get("enabled"):
        raise ValueError("analysis_model_required")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    path = Path(output_dir).resolve() / f"memory-validation-{run_id}.json"
    result = {
        "version": 1, "run_id": run_id,
        "manifest": manifest_path.name,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "model": {key: status.get(key) for key in
                  ("model", "base_url", "api", "version", "output_protocol",
                   "reasoning_effort")},
        "cases": {},
    }
    identity = model_identity(engine)
    _write_result(path, result)
    for case in cases:
        entry = {
            "repo_id": case["context"]["repo_id"],
            "revision": case["context"]["revision"],
            "file_sha256": case["file_sha256"], "baseline": None, "memory": None,
            "summary": None, "score": None,
        }
        result["cases"][case["id"]] = entry
        _write_result(path, result)
        for name, memories in (("baseline", []), ("memory", case["memories"])):
            try:
                if model_identity(engine) == identity:
                    entry[name] = await run_variant(engine, case["context"], memories)
                else:
                    entry[name] = {
                        "status": "failed", "error_code": "analysis_model_changed",
                        "memories": memories, "model_calls": 0, "elapsed_seconds": 0,
                    }
            except asyncio.CancelledError:
                entry[name] = {
                    "status": "failed", "error_code": "memory_evaluation_interrupted",
                    "memories": memories, "model_calls": 0, "elapsed_seconds": 0,
                }
                entry["summary"] = _summary(entry["baseline"], entry["memory"])
                entry["score"] = _score(
                    case["gold"], entry["baseline"], entry["memory"], entry["summary"]
                )
                _write_result(path, result)
                raise
            entry["summary"] = _summary(entry["baseline"], entry["memory"])
            entry["score"] = _score(
                case["gold"], entry["baseline"], entry["memory"], entry["summary"]
            )
            _write_result(path, result)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", help="Run one fixture case before the complete suite")
    args = parser.parse_args()
    model = AnalysisModel(ReadOnlyStore(args.db), GitHubAccounts(ReadOnlyStore(args.db)))
    print(asyncio.run(run_suite(args.cases, args.output_dir, model, selected=args.case)))


if __name__ == "__main__":
    main()
