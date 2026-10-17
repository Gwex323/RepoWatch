"""Bounded historical claims, never current truth or human authorization."""

import json

from .diagnosis import redact_values

MAX_HISTORICAL_FINDINGS = 8


def historical_snapshot(db, repo_id, analysis_id, created):
    """Freeze same-repository unresolved claims before sending any model request."""
    where = (
        "FROM analysis_findings f JOIN repository_analyses a ON a.id=f.analysis_id "
        "WHERE a.repo_id=? AND a.id<>? AND a.created<=? AND a.status='completed' "
        "AND f.status IN ('confirmed','waiting_reproduction') "
    )
    args = (repo_id, analysis_id, created)
    total = db.execute("SELECT COUNT(*) " + where, args).fetchone()[0]
    rows = db.execute(
        "SELECT f.id,f.version,f.status,f.data,a.id AS analysis_id,a.revision " + where +
        "ORDER BY CASE f.status WHEN 'confirmed' THEN 0 ELSE 1 END,a.created,f.id LIMIT ?",
        (*args, MAX_HISTORICAL_FINDINGS),
    ).fetchall()
    items = []
    for row in rows:
        original = json.loads(row["data"])
        # Legacy records may have unbounded reproduction strings. Retain an explicit
        # clipping flag, rather than passing large user/repository payloads through.
        data = {key: str(original.get(key, ""))[:limit] for key, limit in (
            ("title", 200), ("severity", 2), ("explanation", 1200),
            ("expected", 600), ("suspected_actual", 600),
        )}
        data["reproduction"] = [str(step)[:240] for step in original.get("reproduction", [])[:8]]
        data["evidence"] = [{
            "path": str(evidence.get("path", ""))[:240],
            "start_line": evidence.get("start_line"), "end_line": evidence.get("end_line"),
            "quote": str(evidence.get("quote", ""))[:400],
        } for evidence in original.get("evidence", [])[:5]]
        items.append({
            "finding_id": row["id"], "version": row["version"],
            "human_status": row["status"], "analysis_id": row["analysis_id"],
            "revision": row["revision"], "data": data, "content_clipped": data != original,
        })
    return redact_values({"total": total, "included": len(items),
                          "omitted": total - len(items), "items": items})


def historical_results(context, report):
    """Join immutable origin metadata for display; never mutate original records."""
    snapshot = context.get("historical_findings")
    if snapshot is None or report is None:
        return None
    outcomes = {item["finding_id"]: item for item in report.get("historical_rechecks", [])}
    return {**snapshot, "items": [
        {**origin, "result": outcomes.get(origin["finding_id"])}
        for origin in snapshot["items"]
    ]}
