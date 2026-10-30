"""Private, unaccepted correction state bound to a trusted review checkpoint."""

import json

from .diagnosis import redact_values
from .report_evidence_correction import CORRECTION_ERRORS
from .report_recovery import MAX_OUTPUT_TOKENS, digest
from .report_scope_correction import MAPPING_CORRECTION_ERRORS

PROTOCOL = 1
MAX_CORRECTION_BYTES = 4_000_000
KINDS = {"evidence_correction", "scope_mapping_correction"}
FEEDBACK = CORRECTION_ERRORS | MAPPING_CORRECTION_ERRORS | {
    "", "invalid_json", "schema_mismatch", "evidence_correction_invalid",
    "scope_correction_invalid",
}


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS analysis_review_corrections (
            analysis_id TEXT PRIMARY KEY REFERENCES analysis_review_checkpoints(analysis_id)
                ON DELETE CASCADE,
            binding TEXT NOT NULL, checkpoint_digest TEXT NOT NULL,
            payload TEXT NOT NULL, payload_digest TEXT NOT NULL, created REAL NOT NULL
        )
    """)


def valid_state(value):
    return (
        isinstance(value, dict)
        and set(value) == {
            "protocol", "accepted", "kind", "candidate", "feedback", "max_tokens", "targets",
        }
        and type(value["protocol"]) is int and value["protocol"] == PROTOCOL
        and value["accepted"] is False
        and isinstance(value["kind"], str) and value["kind"] in KINDS
        and isinstance(value["candidate"], dict)
        and isinstance(value["feedback"], str) and value["feedback"] in FEEDBACK
        and isinstance(value["targets"], list) and 1 <= len(value["targets"]) <= 112
        and type(value["max_tokens"]) is int and 1 <= value["max_tokens"] <= MAX_OUTPUT_TOKENS
    )


def save(db, analysis_id, checkpoint, value, now):
    # Callers normalize/redact before validating the candidate, so persisting it
    # cannot change which source quotes were actually checked.
    if not valid_state(value) or redact_values(value) != value:
        raise ValueError("analysis_correction_checkpoint_invalid")
    payload = json.dumps(value, ensure_ascii=False)
    if len(payload.encode()) > MAX_CORRECTION_BYTES:
        raise ValueError("analysis_correction_checkpoint_invalid")
    db.execute(
        "INSERT INTO analysis_review_corrections VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(analysis_id) DO UPDATE SET binding=excluded.binding,"
        "checkpoint_digest=excluded.checkpoint_digest,payload=excluded.payload,"
        "payload_digest=excluded.payload_digest,created=excluded.created",
        (analysis_id, checkpoint["binding"], checkpoint["payload_digest"],
         payload, digest(value), now),
    )


def load(db, analysis_id, checkpoint):
    saved = db.execute("SELECT * FROM analysis_review_corrections WHERE analysis_id=?",
                       (analysis_id,)).fetchone()
    if not saved or not checkpoint:
        return None
    try:
        if (saved["binding"] != checkpoint["binding"]
                or saved["checkpoint_digest"] != checkpoint["payload_digest"]
                or len(saved["payload"].encode()) > MAX_CORRECTION_BYTES):
            return None
        value = json.loads(saved["payload"])
        if valid_state(value) and digest(value) == saved["payload_digest"]:
            return value
    except (TypeError, ValueError, RecursionError):
        pass
    return None


def clear(db, analysis_id):
    db.execute("DELETE FROM analysis_review_corrections WHERE analysis_id=?", (analysis_id,))
