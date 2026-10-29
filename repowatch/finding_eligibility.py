"""Server-owned repair provenance, separate from human reproduction feedback."""

import hashlib
import json
import time

POLICY = "analysis_candidates_v1"
ACTIVE = ("repair_candidate", "confirmed")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS finding_repair_origins (
            finding_id TEXT PRIMARY KEY REFERENCES analysis_findings(id),
            kind TEXT NOT NULL CHECK(kind IN
                ('analysis_review','legacy_owner','acceptance_followup','current_recheck')),
            policy TEXT NOT NULL, analysis_id TEXT NOT NULL REFERENCES repository_analyses(id),
            revision TEXT NOT NULL, fingerprint TEXT NOT NULL, trigger_key TEXT NOT NULL,
            finding_digest TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL
        );
    """)


def register(db, analysis, finding_id, kind, evidence):
    """Called only by validated analysis/acceptance transactions, never client input."""
    finding = db.execute(
        "SELECT * FROM analysis_findings WHERE id=? AND analysis_id=?",
        (finding_id, analysis["id"]),
    ).fetchone()
    if not finding or finding["status"] not in ACTIVE:
        return
    if kind == "analysis_review" and finding["status"] != "repair_candidate":
        return
    if kind == "analysis_review":
        # Scope reassessment can replace the live report. Keep its original reviewed
        # source immutable rather than rewriting provenance or invalidating descendants.
        report = db.execute("SELECT report FROM repository_analyses WHERE id=?",
                            (analysis["id"],)).fetchone()[0]
        evidence = {**evidence, "report": json.loads(report or "null")}
    db.execute(
        "INSERT OR IGNORE INTO finding_repair_origins VALUES(?,?,?,?,?,?,?,?,?,?)",
        (finding_id, kind, POLICY, analysis["id"], analysis["revision"],
         analysis["fingerprint"], analysis["trigger_key"], digest(json.loads(finding["data"])),
         json.dumps(evidence, sort_keys=True), time.time()),
    )


def origin(db, analysis, finding, *, historical=False, _seen=()):
    """Return trusted provenance; callers additionally check live analysis/execution basis."""
    if (not analysis or not finding or analysis["status"] != "completed"
            or finding["id"] in _seen or len(_seen) > 8
            or finding["analysis_id"] != analysis["id"]):
        return None
    allowed = (*ACTIVE, "resolved") if historical else ACTIVE
    if finding["status"] not in allowed:
        return None
    saved = db.execute(
        "SELECT * FROM finding_repair_origins WHERE finding_id=?", (finding["id"],),
    ).fetchone()
    if saved:
        if (saved["policy"] != POLICY
                or any(saved[key] != analysis[key] for key in (
                    "revision", "fingerprint", "trigger_key"))
                or saved["analysis_id"] != analysis["id"]
                or saved["finding_digest"] != digest(json.loads(finding["data"]))):
            return None
        evidence = json.loads(saved["evidence"])
        if saved["kind"] == "analysis_review":
            report = evidence.get("report")
            if not report or evidence.get("report_digest") != digest(report):
                return None
        elif saved["kind"] == "current_recheck":
            parent = db.execute("SELECT * FROM analysis_findings WHERE id=? AND analysis_id=?",
                                (evidence.get("parent_finding_id"),
                                 evidence.get("source_analysis_id"))).fetchone()
            report = json.loads(analysis["report"] or "{}")
            recheck = next((item for item in report.get("historical_rechecks", [])
                            if item.get("finding_id") == evidence.get("parent_finding_id")), None)
            if (not parent or parent["status"] not in (
                    *ACTIVE, "waiting_reproduction", "superseded")
                    or evidence.get("parent_digest") != digest(json.loads(parent["data"]))
                    or not recheck or recheck.get("outcome") != "still_present"
                    or not recheck.get("evidence")
                    or evidence.get("recheck_digest") != digest(recheck)):
                return None
        elif saved["kind"] == "acceptance_followup":
            parent = db.execute("SELECT * FROM analysis_findings WHERE id=? AND analysis_id=?",
                                (evidence.get("parent_finding_id"), analysis["id"])).fetchone()
            check = db.execute(
                "SELECT c.* FROM scope_acceptance_checks c "
                "JOIN scope_acceptance_runs r ON r.id=c.run_id "
                "JOIN repair_tasks t ON t.id=c.repair_id "
                "WHERE c.id=? AND c.run_id=? AND r.analysis_id=? "
                "AND t.finding_id=? AND c.item_key=?",
                (evidence.get("check_id"), evidence.get("run_id"), analysis["id"],
                 evidence.get("parent_finding_id"), evidence.get("item_key")),
            ).fetchone()
            if (not parent or not origin(db, analysis, parent, _seen=(*_seen, finding["id"]))
                    or not check or check["state"] not in ("failed", "passed")
                    or evidence.get("result_digest") != digest(
                        json.loads(check["result"] or "null"))
                    or evidence.get("review_digest") != digest(
                        json.loads(check["review"] or "null"))):
                return None
        else:
            # A legacy confirmation is trusted only through its real feedback below.
            return None
        return {"kind": saved["kind"], "policy": saved["policy"],
                "analysis_id": saved["analysis_id"], "revision": saved["revision"],
                "evidence": {key: value for key, value in evidence.items() if key != "report"}}
    # Read existing real owner feedback without mutating or activating old pending findings.
    feedback = json.loads(finding["feedback"] or "{}")
    if (finding["status"] in (("confirmed", "resolved") if historical else ("confirmed",))
            and feedback.get("decision") in (
                ("confirmed", "resolved") if historical else ("confirmed",))
            and feedback.get("actor") == "owner"
            and feedback.get("revision") == analysis["revision"]):
        return {"kind": "legacy_owner", "policy": "legacy_owner",
                "analysis_id": analysis["id"], "revision": analysis["revision"],
                "evidence": feedback}
    return None
