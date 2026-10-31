"""Durable usage and backoff for final-review continuations without quotas."""

import json

MAX_REQUESTS = None
MAX_SECONDS = None
NO_PROGRESS_LIMIT = 2
RECOVERY_DELAYS = (10, 30, 60)
RETRY_FIELDS = ("used", "output_used", "format_used", "evidence_used",
                "required_read_used", "tool_arguments_used")
RECOVERABLE = frozenset({
    "analysis_json_invalid", "analysis_report_invalid", "analysis_provider_timeout_exhausted",
    "analysis_provider_timeout", "analysis_provider_unreachable", "analysis_output_truncated",
    "analysis_deadline_exceeded", "analysis_interrupted", "analysis_service_stopped",
    "analysis_evidence_unverified", "scope_mapping_invalid", "scope_review_incomplete",
})


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS analysis_report_recovery (
            analysis_id TEXT PRIMARY KEY REFERENCES repository_analyses(id),
            state TEXT NOT NULL, started REAL NOT NULL, deadline REAL NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, resumptions INTEGER NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0, retry_budget TEXT NOT NULL DEFAULT '{}',
            diagnostics TEXT NOT NULL DEFAULT '{}', stage TEXT, binding TEXT,
            last_error TEXT, stop_reason TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS analysis_report_recovery_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL REFERENCES repository_analyses(id),
            snapshot TEXT NOT NULL, created REAL NOT NULL
        )
    """)


def begin(db, analysis_id, now):
    # Only a newly claimed run opts in. Installing this table cannot restart old failures.
    db.execute(
        "INSERT OR IGNORE INTO analysis_report_recovery(analysis_id,state,started,deadline) "
        "VALUES(?,'active',?,?)", (analysis_id, now, 0),
    )
    return row(db, analysis_id)


def row(db, analysis_id):
    return db.execute("SELECT * FROM analysis_report_recovery WHERE analysis_id=?",
                      (analysis_id,)).fetchone()


def new_scope_review(db, analysis_id, now):
    """An explicit owner scope reassessment is a new task, never an automatic retry."""
    saved = row(db, analysis_id)
    if saved is not None:
        db.execute(
            "INSERT INTO analysis_report_recovery_history(analysis_id,snapshot,created) "
            "VALUES(?,?,?)", (analysis_id, json.dumps(dict(saved)), now),
        )
        db.execute("DELETE FROM analysis_report_recovery WHERE analysis_id=?", (analysis_id,))
        db.execute(
            "INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
            (analysis_id, "report_recovery_new_scope", "owner_reassessment", now),
        )


def limit_reason(saved, now):
    """Legacy deadlines and counts are accounting history, never a stop condition."""
    return None


def save_budget(db, analysis_id, value):
    # Counters never decrease, even after service restart or explicit owner retry.
    previous = json.loads(row(db, analysis_id)["retry_budget"])
    merged = {key: max(previous.get(key, 0), value.get(key, 0)) for key in RETRY_FIELDS}
    db.execute("UPDATE analysis_report_recovery SET retry_budget=? WHERE analysis_id=?",
               (json.dumps(merged), analysis_id))


def public_state(saved, now):
    if saved is None:
        return None
    return {
        "state": saved["state"], "requests": saved["requests"], "max_requests": MAX_REQUESTS,
        "resumptions": saved["resumptions"], "max_resumptions": None,
        "retry_after": saved["retry_after"], "deadline": None,
        "remaining_seconds": None,
        "last_error": saved["last_error"], "stop_reason": saved["stop_reason"],
        "retry_budget": json.loads(saved["retry_budget"]),
    }


def repeated_no_progress(db, analysis_id, code):
    """Return True when this error's latest safe fingerprint has already occurred."""
    row = db.execute(
        "WITH documents AS ("
        "SELECT id, CASE WHEN json_valid(agent) THEN agent ELSE '{}' END AS document "
        "FROM analysis_events WHERE analysis_id=? AND kind='failure'), "
        "fingerprints AS ("
        "SELECT id, json_extract(document, '$.code') AS code, "
        "json_extract(document, '$.progress_fingerprint') AS fingerprint "
        "FROM documents WHERE json_type(document)='object') "
        "SELECT fingerprint, COUNT(*) AS occurrences FROM fingerprints "
        "WHERE code=? AND fingerprint IS NOT NULL AND fingerprint <> '' "
        "GROUP BY fingerprint ORDER BY MAX(id) DESC LIMIT 1",
        (analysis_id, code),
    ).fetchone()
    return row is not None and row["occurrences"] >= NO_PROGRESS_LIMIT
