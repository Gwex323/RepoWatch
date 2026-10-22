"""Shared current-revision and execution-evidence identity for analysis and scope gates."""

import hashlib
import json

from .diagnosis import evidence_matches, fingerprint


def execution_trigger(digest, job_ids, pipeline_id=None, issue_id=None):
    payload = [digest, job_ids]
    if pipeline_id:
        payload.append(pipeline_id)
    if issue_id:
        payload.append(issue_id)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def analysis_current(db, analysis):
    env = db.execute("SELECT * FROM environments WHERE id=?", (analysis["env_id"],)).fetchone()
    if not env or env["state"] == "deleted" or fingerprint(
        json.loads(env["config"] or "{}"), env["revision"],
    ) != analysis["fingerprint"]:
        return False
    latest, seen = [], set()
    for job in db.execute(
        "SELECT id,action,context,error FROM execution_jobs WHERE env_id=? "
        "AND action IN ('setup','test','start') AND state IN ('completed','failed') "
        "ORDER BY created DESC LIMIT 20", (analysis["env_id"],),
    ):
        context = json.loads(job["context"]) if job["context"] else None
        if (job["action"] in seen or not context or job["error"] in (
            "execution_interrupted", "execution_cancelled", "cleanup_pending",
        ) or not evidence_matches(context, json.loads(env["config"] or "{}"),
                                  env["revision"], job["action"])):
            continue
        seen.add(job["action"])
        latest.append(job["id"])
    context = json.loads(analysis["context"] or "{}")
    trigger = execution_trigger(
        analysis["fingerprint"], latest, context.get("pipeline_id"),
        context.get("issue", {}).get("id") if isinstance(context.get("issue"), dict) else None,
    )
    return trigger == analysis["trigger_key"]
