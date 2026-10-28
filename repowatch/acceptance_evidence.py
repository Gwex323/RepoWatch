"""Strict source evidence checks with value-free diagnostics and bounded correction hints."""

import json

from .repository_analysis import evidence_issue
from .store import Conflict

MAX_ERRORS = 12
REASONS = {
    "unknown_file", "unread_file", "invalid_range", "unread_range", "quote_mismatch",
    "missing_current_evidence",
}
FIELDS = {"scope_review", "items", "exclusions", "evidence"}


class AcceptanceEvidenceError(Conflict):
    def __init__(self, errors):
        super().__init__("acceptance_evidence_invalid")
        # Only static field names, bounded list positions and fixed reasons may persist.
        self.validation = {"errors": [
            {"path": [part if isinstance(part, str) and part in FIELDS
                      or type(part) is int and 0 <= part < 1000 else "unknown_field"
                      for part in error["path"][:8]],
             "reason": error["reason"] if error["reason"] in REASONS else "invalid_evidence"}
            for error in errors[:MAX_ERRORS]
        ]}


def validate_scope_evidence(review, files):
    """Collect source issues without changing or relaxing the existing evidence validator."""
    ranges = {path: [[1, len(text.splitlines())]] for path, text in files.items()}
    errors = []
    for section in ("items", "exclusions"):
        for index, item in enumerate(getattr(review, section)):
            path = ["scope_review", section, index, "evidence"]
            if item.outcome in ("no_defect", "unrelated") and not item.evidence:
                errors.append({"path": path, "reason": "missing_current_evidence"})
            for evidence_index, evidence in enumerate(item.evidence):
                reason = evidence_issue(evidence, {"files": files}, set(files), ranges)
                if reason:
                    errors.append({"path": [*path, evidence_index], "reason": reason})
            if len(errors) >= MAX_ERRORS:
                break
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        raise AcceptanceEvidenceError(errors)


def correction_hints(error, review, files):
    """Untrusted source excerpts go to this correction call only, never to saved diagnostics."""
    hints, remaining = [], 16000
    for issue in error.validation["errors"]:
        path = issue["path"]
        hint = {"path": path}
        if len(path) != 5:
            continue
        evidence = getattr(review, path[1])[path[2]].evidence[path[4]]
        if evidence.path not in files:
            # Never echo a proposed unknown path or fetch source outside the original packet.
            hint["available_files"] = list(files)[:64]
        else:
            lines = files[evidence.path].splitlines()
            text = "\n".join(lines)
            matches, offset = [], 0
            if evidence.quote.strip():
                for _ in range(3):
                    start = text.find(evidence.quote, offset)
                    if start < 0:
                        break
                    matches.append({"start_line": text.count("\n", 0, start) + 1,
                                    "end_line": text.count("\n", 0, start + len(
                                        evidence.quote)) + 1})
                    offset = start + 1
            anchor = matches[0]["start_line"] if matches else evidence.start_line
            if not 1 <= anchor <= len(lines):
                anchor = 1
            first = max(1, anchor - 3)
            hint.update({
                "source_path": evidence.path, "line_count": len(lines),
                "exact_quote_matches": matches,
                "source_lines": [{"line": index + 1, "text": lines[index]}
                                 for index in range(first - 1, min(len(lines), first + 23))],
            })
        size = len(json.dumps(hint, ensure_ascii=False))
        if size <= remaining:
            hints.append(hint)
            remaining -= size
    return hints
