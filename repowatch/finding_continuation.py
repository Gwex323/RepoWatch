"""Explicit owner continuation of a confirmed historical claim on a fresh revision."""

import json
import time

from .repair_origin import analysis_origin
from .store import Conflict, Missing, uid
from .task_scopes import request_scope_review, scope_snapshot


def continue_finding(analyses, analysis_id, finding_id, version, target_analysis_id,
                     analysis_version, note):
    from .repository_analysis import Finding, source_snapshot, validate_report

    if not note.strip() or analyses.repairer is None:
        raise Conflict("finding_continuation_required")
    with analyses.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM analysis_findings WHERE id=? AND analysis_id=?",
                         (finding_id, analysis_id)).fetchone()
        source = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                            (analysis_id,)).fetchone()
        target = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                            (target_analysis_id,)).fetchone()
        if not old or not source or not target:
            raise Missing("Finding or analysis not found.")
        if old["version"] != version or target["version"] != analysis_version:
            raise Conflict("finding_changed")
        confirmation = json.loads(old["feedback"] or "{}")
        if (old["status"] != "confirmed" or confirmation.get("decision") != "confirmed"
                or confirmation.get("actor") != "owner"
                or confirmation.get("revision") != source["revision"]):
            raise Conflict("repair_confirmation_required")
        if (source["status"] != "completed" or target["status"] != "completed"
                or source["repo_id"] != target["repo_id"] or source["id"] == target["id"]
                or source["created"] >= target["created"] or not analyses.current(target)):
            raise Conflict("analysis_stale")
        latest = db.execute("SELECT id FROM repository_analyses WHERE repo_id=? "
                            "ORDER BY created DESC LIMIT 1", (target["repo_id"],)).fetchone()
        if latest["id"] != target_analysis_id:
            raise Conflict("analysis_stale")
        if analysis_origin(db, source) != analysis_origin(db, target):
            raise Conflict("repair_issue_reference_invalid")
        if db.execute(
            "SELECT 1 FROM repair_tasks WHERE (finding_id=? OR analysis_id=?) "
            "AND (status='running' OR remote IS NOT NULL)", (finding_id, target_analysis_id),
        ).fetchone() or db.execute(
            "SELECT 1 FROM execution_jobs WHERE env_id=? AND state IN ('queued','running')",
            (target["env_id"],),
        ).fetchone():
            raise Conflict("repair_running")
        context = json.loads(target["context"])
        origin = next((item for item in context.get("historical_findings", {}).get("items", [])
                       if item["finding_id"] == finding_id), None)
        if not origin or origin["version"] != version or origin["analysis_id"] != analysis_id:
            raise Conflict("finding_recheck_required")
        if source_snapshot(analyses.execution.directory(target["env_id"]) / "source")["files"] != (
            context.get("files")
        ):
            raise Conflict("analysis_stale")
        review = validate_report(json.loads(target["report"]), context)
        recheck = next((item for item in review.historical_rechecks
                        if item.finding_id == finding_id), None)
        if not recheck or recheck.outcome != "still_present" or not recheck.evidence:
            raise Conflict("finding_recheck_required")
        paths = {item.path for item in recheck.evidence}
        scope = scope_snapshot(db, target_analysis_id)
        conclusions = {item.key: item for item in review.scope_review.items} \
            if review.scope_review else {}
        keys = [item["key"] for item in scope["items"]
                if paths & set(item["paths"]) and item["key"] in conclusions
                and conclusions[item["key"]].outcome == "confirmed"]
        if not keys:
            raise Conflict("finding_scope_required")
        data = Finding.model_validate({**json.loads(old["data"]), "scope_keys": keys,
                                      "evidence": [item.model_dump() for item in recheck.evidence]})
        successor, now = uid("finding"), time.time()
        feedback = {"decision": "confirmed", "actor": "owner", "revision": target["revision"],
                    "note": note.strip(), "at": now, "continued_from": finding_id,
                    "original_analysis_id": analysis_id, "original_confirmation": confirmation,
                    "recheck_analysis_id": target_analysis_id}
        db.execute(
            "INSERT INTO analysis_findings(id,analysis_id,status,data,feedback) "
            "VALUES(?,?,'confirmed',?,?)", (successor, target_analysis_id,
                                          data.model_dump_json(), json.dumps(feedback)),
        )
        for key in keys:
            db.execute("INSERT INTO task_scope_findings VALUES(?,?,?)",
                       (scope["id"], key, successor))
        # Adding a carried finding invalidates overall scope acceptance. A future
        # publication remains partial until independent scope review is renewed.
        context = request_scope_review(db, target)
        db.execute("UPDATE repository_analyses SET context=?,version=version+1,updated=? "
                   "WHERE id=?",
                   (json.dumps(context), now, target_analysis_id))
        db.execute(
            "UPDATE analysis_findings SET status='superseded',feedback=?,version=version+1 "
            "WHERE id=?", (json.dumps({"decision": "continued", "actor": "owner", "at": now,
                                      "note": note.strip(), "continued_as": successor,
                                      "analysis_id": target_analysis_id,
                                      "previous_feedback": confirmation}), finding_id),
        )
        analyses.repairer.cancel_pending_in(db, finding_id, decision="continued")
        repair_id = analyses.repairer.enqueue_in(db, target_analysis_id, successor)
        db.execute("INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
                   (target_analysis_id, "finding_continued", finding_id + " -> " + successor, now))
    return {"finding_id": successor, "analysis_id": target_analysis_id, "repair_id": repair_id}
