"""Durable, append-only task requirements and evidence-bound completion decisions."""

import hashlib
import json
import re
import time

from .analysis_basis import analysis_current
from .analysis_tools import tool_allowed
from .diagnosis import redact, redact_values
from .scope_models import ScopeRequirement, ScopeReview, scope_mapping_issues
from .store import Conflict, Missing, uid

MAX_ITEMS = 32
COMPLETE = {"verified_fixed", "verified_no_defect"}


def skipped_tests(log):
    counts = re.findall(r"\bskipped=(\d+)|\b(\d+)\s+skipped\b", log or "")
    return max((int(a or b) for a, b in counts), default=0)


def execution_limits(analysis):
    context = json.loads(analysis["context"] or "{}")
    desktop = (context.get("test_result") or {}).get("desktop_scope") or {}
    exclusions = [{"key": "excluded:" + item["path"], "path": item["path"],
                   "reasons": item["reasons"]} for item in desktop.get("excluded", [])]
    skipped = max(desktop.get("skipped", 0), max((
        skipped_tests(job.get("log", "")) for job in context.get("jobs", [])
        if job.get("action") == "test"
    ), default=0))
    if skipped:
        exclusions.append({"key": "skipped_tests", "path": None,
                           "reasons": ["skipped"], "count": skipped})
    return exclusions


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def original_request(analysis):
    context = json.loads(analysis["context"] or "{}")
    issue = context.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("id"), str):
        original = {key: issue.get(key) for key in (
            "id", "number", "title", "body", "url", "updated_at",
        )}
        original["clipped"] = len(issue.get("body") or "") >= 10000
        return "issue:" + issue["id"], original
    return "analysis:" + analysis["id"], {
        "request": "调查当前仓库的行为缺陷，并核验受影响入口、相似实现及运行限制。",
        "clipped": False,
    }


def _event(db, scope, kind, actor, value):
    db.execute(
        "INSERT INTO task_scope_events(scope_id,version,kind,actor,value,created) "
        "VALUES(?,?,?,?,?,?)",
        (scope["id"], scope["version"], kind, actor,
         json.dumps(redact_values(value), ensure_ascii=False), time.time()),
    )


def ensure_scope(db, analysis):
    """Keep old requirements when a later analysis changes the revision or request."""
    origin, original = original_request(analysis)
    row = db.execute(
        "SELECT * FROM task_scopes WHERE repo_id=? AND origin=?",
        (analysis["repo_id"], origin),
    ).fetchone()
    now = time.time()
    if row is None:
        scope_id = uid("scope")
        db.execute(
            "INSERT INTO task_scopes(id,repo_id,origin,analysis_id,revision,original,"
            "original_digest,status,created,updated) VALUES(?,?,?,?,?,?,?,'draft',?,?)",
            (scope_id, analysis["repo_id"], origin, analysis["id"], analysis["revision"],
             json.dumps(original, ensure_ascii=False), digest(original), now, now),
        )
        row = db.execute("SELECT * FROM task_scopes WHERE id=?", (scope_id,)).fetchone()
        _event(db, row, "created", "backend", {"analysis_id": analysis["id"]})
    elif row["analysis_id"] != analysis["id"]:
        previous = db.execute(
            "SELECT created FROM repository_analyses WHERE id=?", (row["analysis_id"],),
        ).fetchone()
        if not previous or analysis["created"] >= previous["created"]:
            db.execute(
                "UPDATE task_scopes SET analysis_id=?,revision=?,original=?,original_digest=?,"
                "status='draft',version=version+1,updated=? WHERE id=?",
                (analysis["id"], analysis["revision"], json.dumps(original, ensure_ascii=False),
                 digest(original), now, row["id"]),
            )
            row = db.execute("SELECT * FROM task_scopes WHERE id=?", (row["id"],)).fetchone()
            _event(db, row, "analysis_changed", "backend", {
                "analysis_id": analysis["id"], "original": original,
            })
    db.execute(
        "INSERT OR IGNORE INTO analysis_task_scopes(analysis_id,scope_id) VALUES(?,?)",
        (analysis["id"], row["id"]),
    )
    return row


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS task_scopes (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
            origin TEXT NOT NULL, analysis_id TEXT NOT NULL REFERENCES repository_analyses(id),
            revision TEXT NOT NULL, original TEXT NOT NULL, original_digest TEXT NOT NULL,
            status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(repo_id,origin)
        );
        CREATE TABLE IF NOT EXISTS analysis_task_scopes (
            analysis_id TEXT PRIMARY KEY REFERENCES repository_analyses(id),
            scope_id TEXT NOT NULL REFERENCES task_scopes(id)
        );
        CREATE TABLE IF NOT EXISTS task_scope_items (
            scope_id TEXT NOT NULL REFERENCES task_scopes(id), key TEXT NOT NULL,
            requirement TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope_id,key)
        );
        CREATE TABLE IF NOT EXISTS task_scope_reviews (
            scope_id TEXT NOT NULL REFERENCES task_scopes(id), version INTEGER NOT NULL,
            analysis_id TEXT NOT NULL REFERENCES repository_analyses(id),
            review TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope_id,version)
        );
        CREATE TABLE IF NOT EXISTS task_scope_findings (
            scope_id TEXT NOT NULL REFERENCES task_scopes(id), item_key TEXT NOT NULL,
            finding_id TEXT NOT NULL REFERENCES analysis_findings(id),
            PRIMARY KEY(scope_id,item_key,finding_id)
        );
        CREATE TABLE IF NOT EXISTS task_scope_evidence (
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES task_scopes(id),
            item_key TEXT NOT NULL, version INTEGER NOT NULL, revision TEXT NOT NULL,
            kind TEXT NOT NULL, source TEXT NOT NULL, reference TEXT NOT NULL,
            patch_digest TEXT, passed INTEGER NOT NULL, details TEXT NOT NULL,
            created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS scope_evidence_lookup
            ON task_scope_evidence(scope_id,version,item_key);
        CREATE TABLE IF NOT EXISTS task_scope_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL REFERENCES task_scopes(id), version INTEGER NOT NULL,
            kind TEXT NOT NULL, actor TEXT NOT NULL, value TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_scope_publications (
            repair_id TEXT PRIMARY KEY REFERENCES repair_tasks(id),
            scope_id TEXT NOT NULL REFERENCES task_scopes(id), version INTEGER NOT NULL,
            revision TEXT NOT NULL, patch_digest TEXT NOT NULL,
            mode TEXT NOT NULL, snapshot TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS scope_requirement_immutable
            BEFORE UPDATE ON task_scope_items BEGIN
            SELECT RAISE(ABORT, 'scope_requirement_immutable'); END;
        CREATE TRIGGER IF NOT EXISTS scope_requirement_retained
            BEFORE DELETE ON task_scope_items BEGIN
            SELECT RAISE(ABORT, 'scope_requirement_retained'); END;
        CREATE TRIGGER IF NOT EXISTS scope_evidence_immutable
            BEFORE UPDATE ON task_scope_evidence BEGIN
            SELECT RAISE(ABORT, 'scope_evidence_immutable'); END;
    """)
    for analysis in db.execute(
        "SELECT a.* FROM repository_analyses a LEFT JOIN analysis_task_scopes s "
        "ON s.analysis_id=a.id WHERE s.analysis_id IS NULL ORDER BY a.created"
    ).fetchall():
        ensure_scope(db, analysis)


def _bound(db, analysis_id):
    row = db.execute(
        "SELECT s.* FROM task_scopes s JOIN analysis_task_scopes a ON a.scope_id=s.id "
        "WHERE a.analysis_id=?", (analysis_id,),
    ).fetchone()
    if not row:
        raise Missing("Task scope not found.")
    return row


def _requirements(db, scope):
    return [json.loads(item[0]) for item in db.execute(
        "SELECT requirement FROM task_scope_items WHERE scope_id=? ORDER BY created,key",
        (scope["id"],),
    )]


def current_scope_findings(db, analysis_id):
    return [{"id": row["id"], "status": row["status"], "data": json.loads(row["data"])}
            for row in db.execute("SELECT * FROM analysis_findings WHERE analysis_id=?",
                                  (analysis_id,))]


def request_scope_review(db, analysis):
    scope = _bound(db, analysis["id"])
    if scope["analysis_id"] != analysis["id"]:
        raise Conflict("scope_changed")
    context = json.loads(analysis["context"])
    issue = context.get("issue")
    if isinstance(issue, dict):
        cached = db.execute("SELECT * FROM github_issues WHERE id=? AND repo_id=?",
                            (issue.get("id"), analysis["repo_id"])).fetchone()
        if not cached:
            raise Conflict("scope_changed")
        context["issue"] = {key: cached[key] for key in (
            "id", "repo_id", "number", "title", "body", "state", "author", "url", "updated_at",
        )}
    context["current_scope_findings"] = current_scope_findings(db, analysis["id"])
    _, original = original_request({**dict(analysis), "context": json.dumps(context)})
    db.execute(
        "UPDATE task_scopes SET original=?,original_digest=?,status='draft',"
        "version=version+1,updated=? WHERE id=?",
        (json.dumps(original, ensure_ascii=False), digest(original), time.time(), scope["id"]),
    )
    _event(db, _bound(db, analysis["id"]), "reassessment_requested", "owner", {})
    return context


def register_scope(db, analysis, proposals, actor):
    if actor != "owner" and not tool_allowed(actor, "register_task_scope"):
        raise Conflict("scope_role_required")
    scope = _bound(db, analysis["id"])
    allowed_status = ("completed", "failed", "interrupted") if actor == "owner" else ("running",)
    if scope["analysis_id"] != analysis["id"] or analysis["status"] not in allowed_status:
        raise Conflict("scope_changed")
    existing = {item["key"]: item for item in _requirements(db, scope)}
    files = json.loads(analysis["context"]).get("files", {})
    additions = []
    for raw in proposals:
        item = redact_values(ScopeRequirement.model_validate(raw).model_dump())
        item["required_evidence"] = sorted(set(item["required_evidence"]))
        item["paths"] = sorted(set(item["paths"]))
        if any(path not in files for path in item["paths"]):
            raise Conflict("scope_path_invalid")
        if item["key"] in existing:
            if existing[item["key"]] != item:
                raise Conflict("scope_requirement_immutable")
            continue
        existing[item["key"]] = item
        additions.append(item)
    if len(existing) > MAX_ITEMS:
        raise Conflict("scope_limit")
    if additions:
        for item in additions:
            db.execute("INSERT INTO task_scope_items VALUES(?,?,?,?)", (
                scope["id"], item["key"], json.dumps(item, ensure_ascii=False), time.time(),
            ))
        db.execute(
            "UPDATE task_scopes SET version=version+1,status='draft',updated=? WHERE id=?",
            (time.time(), scope["id"]),
        )
        scope = _bound(db, analysis["id"])
        _event(db, scope, "requirements_added", actor, additions)
    return scope_packet(db, analysis["id"])


def save_scope_review(db, analysis, review, findings):
    """Only the final, evidence-validated independent analysis review freezes scope."""
    scope = _bound(db, analysis["id"])
    if scope["analysis_id"] != analysis["id"]:
        raise Conflict("scope_changed")
    requirements = {item["key"]: item for item in _requirements(db, scope)}
    parsed = ScopeReview.model_validate(review).model_dump() if review is not None else None
    allowed = {row[0] for row in db.execute(
        "SELECT id FROM analysis_findings WHERE analysis_id=?", (analysis["id"],),
    )}
    if scope_mapping_issues([data for _, data in findings], parsed, requirements, allowed):
        raise Conflict("scope_mapping_invalid")
    for finding_id, data in findings:
        for key in set(data.get("scope_keys", [])):
            db.execute("INSERT OR IGNORE INTO task_scope_findings VALUES(?,?,?)",
                       (scope["id"], key, finding_id))
    if review is None:
        return
    keys = [item["key"] for item in parsed["items"]]
    if (parsed["version"] != scope["version"] or set(keys) != set(requirements)
            or len(keys) != len(set(keys))):
        raise Conflict("scope_review_incomplete")
    limits = {item["key"]: item for item in execution_limits(analysis)}
    classifications = {item["key"]: item for item in parsed["exclusions"]}
    if len(classifications) != len(parsed["exclusions"]) or not set(classifications) <= set(limits):
        raise Conflict("scope_review_incomplete")
    for key, item in classifications.items():
        if item["outcome"] == "unrelated" and not item["evidence"]:
            raise Conflict("scope_review_incomplete")
        if item["outcome"] == "required":
            required_kind = "client" if set(limits[key]["reasons"]) & {
                "windows_native", "gui",
            } else "integration"
            if not item["required_keys"] or any(
                target not in requirements
                or required_kind not in requirements[target]["required_evidence"]
                for target in item["required_keys"]
            ):
                raise Conflict("scope_review_incomplete")
    for item in parsed["items"]:
        for finding_id in set(item["finding_ids"]):
            db.execute("INSERT OR IGNORE INTO task_scope_findings VALUES(?,?,?)",
                       (scope["id"], item["key"], finding_id))
    # A finding outside the coverage matrix prevents an overall acceptance claim.
    mapped = not db.execute(
        "SELECT 1 FROM analysis_findings f WHERE f.analysis_id=? AND NOT EXISTS "
        "(SELECT 1 FROM task_scope_findings m WHERE m.finding_id=f.id AND m.scope_id=?)",
        (analysis["id"], scope["id"]),
    ).fetchone()
    accepted = parsed["accepted"] and mapped and not json.loads(scope["original"])["clipped"]
    db.execute(
        "INSERT INTO task_scope_reviews VALUES(?,?,?,?,?)",
        (scope["id"], scope["version"], analysis["id"],
         json.dumps(parsed, ensure_ascii=False), time.time()),
    )
    db.execute("UPDATE task_scopes SET status=?,updated=? WHERE id=?", (
        "reviewed" if accepted else "draft", time.time(), scope["id"],
    ))
    _event(db, scope, "scope_reviewed", "independent_reviewer", parsed)


def _repair_proof(db, scope, evidence):
    task = db.execute("SELECT * FROM repair_tasks WHERE id=?", (evidence["reference"],)).fetchone()
    if not task or task["revision"] != scope["revision"] or not task["patch"]:
        return False
    context = json.loads(task["context"])
    verification = json.loads(task["verification"] or "{}")
    review = json.loads(task["review"] or "{}")
    details = json.loads(evidence["details"])
    return (context.get("task_scope", {}).get("version") == scope["version"]
            and evidence["patch_digest"] == digest(json.loads(task["patch"]))
            and review.get("approved") is True and verification.get("passed") is True
            and review.get("scope_version") == scope["version"]
            and details.get("review_digest") == digest(review)
            and details.get("verification_digest") == digest(verification)
            and task["status"] in ("running", "pr_created", "failed", "interrupted"))


def scope_snapshot(db, analysis_id, *, candidate=None):
    scope = _bound(db, analysis_id)
    analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                          (analysis_id,)).fetchone()
    active = scope["analysis_id"] == analysis_id and analysis_current(db, analysis)
    original = json.loads(scope["original"])
    if scope["origin"].startswith("issue:"):
        cached = db.execute("SELECT * FROM github_issues WHERE id=?",
                            (original["id"],)).fetchone()
        active = active and cached is not None and all(
            cached[key] == original[key] for key in ("title", "body", "url", "updated_at")
        )
    stored_review = db.execute(
        "SELECT review FROM task_scope_reviews WHERE scope_id=? AND version=?",
        (scope["id"], scope["version"]),
    ).fetchone()
    review = json.loads(stored_review[0]) if stored_review else None
    conclusions = {item["key"]: item for item in review["items"]} if review else {}
    limits = execution_limits(analysis)
    classifications = {item["key"]: item for item in review["exclusions"]} if review else {}
    pending_limits = [limit for limit in limits if classifications.get(
        limit["key"], {},
    ).get("outcome", "unknown") == "unknown"]
    accepted = bool(active and scope["status"] == "reviewed"
                    and analysis["status"] == "completed")
    items, counts, candidate_sets, unchanged_paths = [], {}, [], []
    candidate_paths = {}

    def changed_paths(repair_id):
        if repair_id not in candidate_paths:
            row = db.execute("SELECT patch FROM repair_tasks WHERE id=?", (repair_id,)).fetchone()
            patch = json.loads(row[0]) if row and row[0] else {"edits": []}
            candidate_paths[repair_id] = {edit["path"] for edit in patch["edits"]}
        return candidate_paths[repair_id]
    for requirement in _requirements(db, scope):
        key = requirement["key"]
        conclusion = conclusions.get(key)
        findings = [dict(row) for row in db.execute(
            "SELECT f.id,f.status,f.version,f.analysis_id FROM analysis_findings f "
            "JOIN task_scope_findings m ON m.finding_id=f.id WHERE m.scope_id=? AND m.item_key=?",
            (scope["id"], key),
        )]
        current_findings = [f for f in findings if f["analysis_id"] == scope["analysis_id"]]
        evidence = []
        recent = db.execute(
            "SELECT * FROM task_scope_evidence WHERE scope_id=? AND item_key=? "
            "ORDER BY created DESC LIMIT 80", (scope["id"], key),
        ).fetchall()
        current_entries = db.execute(
            "SELECT * FROM task_scope_evidence WHERE scope_id=? AND item_key=? AND version=? "
            "ORDER BY created DESC", (scope["id"], key, scope["version"]),
        ).fetchall()
        latest, selected = {}, {entry["id"]: entry for entry in recent}
        for entry in current_entries:
            identity = (entry["source"], entry["kind"], entry["patch_digest"])
            if identity not in latest:
                latest[identity] = entry["id"]
                selected[entry["id"]] = entry
        for entry in sorted(selected.values(), key=lambda value: value["created"]):
            value = dict(entry)
            identity = (entry["source"], entry["kind"], entry["patch_digest"])
            valid = (accepted and entry["version"] == scope["version"]
                     and entry["revision"] == scope["revision"] and bool(entry["passed"]))
            valid = valid and latest.get(identity) == entry["id"]
            if entry["source"] == "repair":
                valid = valid and _repair_proof(db, scope, entry)
                if candidate is not None:
                    valid = valid and entry["reference"] == candidate
            elif entry["source"] == "owner":
                details = json.loads(entry["details"])
                if entry["patch_digest"]:
                    ref = db.execute("SELECT * FROM repair_tasks WHERE id=?",
                                     (details.get("repair_id"),)).fetchone()
                    valid = (valid and ref is not None and ref["patch"] is not None
                             and digest(json.loads(ref["patch"])) == entry["patch_digest"])
                    if candidate is not None:
                        valid = valid and details.get("repair_id") == candidate
            value.update(valid=bool(valid), details=json.loads(value["details"]))
            value["effective_failure"] = bool(
                accepted and entry["source"] == "owner" and not entry["passed"]
                and entry["version"] == scope["version"] and entry["revision"] == scope["revision"]
                and latest.get(identity) == entry["id"]
            )
            evidence.append(value)
        failed = {(item["patch_digest"], item["kind"]) for item in evidence
                  if item["effective_failure"]}
        for item in evidence:
            if (item["patch_digest"], item["kind"]) in failed:
                item["valid"] = False
        valid = [item for item in evidence if item["valid"]]
        required = set(requirement["required_evidence"])
        repair_kinds = {item["kind"] for item in valid if item["patch_digest"]}
        base_kinds = {item["kind"] for item in valid if item["patch_digest"] is None}
        if candidate is not None and changed_paths(candidate) & set(requirement["paths"]):
            base_kinds = set()
        status = "investigating"
        verified_candidates = set()
        no_defect = (conclusion and conclusion["outcome"] == "no_defect"
                     and conclusion["evidence"] and all(
                         f["status"] in ("not_reproduced", "false_positive")
                         for f in current_findings
                     ))
        if accepted and no_defect and required <= base_kinds:
            status = "verified_no_defect"
            unchanged_paths.append(set(requirement["paths"]))
        elif accepted and required <= repair_kinds and any(
            item["source"] == "repair" and item["kind"] == "regression" for item in valid
        ):
            # All extra observations must describe the same candidate as the repair proof.
            repairs = {item["reference"] for item in valid if item["source"] == "repair"}
            for repair_id in repairs:
                kinds = {item["kind"] for item in valid if (
                    item["source"] == "repair" and item["reference"] == repair_id
                    or item["source"] == "owner"
                    and item["details"].get("repair_id") == repair_id
                )}
                if required <= kinds:
                    status = "verified_fixed"
                    verified_candidates.add(repair_id)
        elif any(f["status"] == "confirmed" for f in current_findings):
            status = "awaiting_verification" if repair_kinds else "confirmed"
        if status not in COMPLETE and conclusion and conclusion["outcome"] == "blocked":
            status = "blocked"
        if status not in COMPLETE and failed:
            status = "blocked"
        if verified_candidates:
            candidate_sets.append(verified_candidates)
        missing = sorted(required - (base_kinds if no_defect else repair_kinds))
        counts[status] = counts.get(status, 0) + 1
        items.append({**requirement, "status": status, "conclusion": conclusion,
                      "findings": findings, "evidence": evidence, "missing_evidence": missing})
    done = sum(counts.get(key, 0) for key in COMPLETE)
    common = set.intersection(*candidate_sets) if candidate_sets else set()
    combined = not candidate_sets or any(
        not any(changed_paths(repair_id) & paths for paths in unchanged_paths)
        for repair_id in common
    )
    complete = accepted and bool(items) and done == len(items) and combined and not pending_limits
    return {"id": scope["id"], "version": scope["version"], "revision": scope["revision"],
            "analysis_id": scope["analysis_id"], "origin": scope["origin"],
            "original": original, "reviewed": accepted,
            "stale": not active, "status": "complete" if complete else (
                "unreviewed" if not accepted else "partial"),
            "complete": complete, "total": len(items), "done": done, "counts": counts,
            "combination_pending": bool(items) and done == len(items) and not combined,
            "execution_limits": limits, "pending_limits": pending_limits,
            "items": items, "review": review}


def scope_packet(db, analysis_id):
    """Keep observation bodies and historical proofs out of model prompt/context growth."""
    snapshot = scope_snapshot(db, analysis_id)
    return {key: value for key, value in snapshot.items() if key not in ("items", "review")} | {
        "items": [{key: value for key, value in item.items()
                   if key not in ("evidence", "findings", "conclusion")}
                  for item in snapshot["items"]],
    }


class TaskScopes:
    def __init__(self, store):
        self.store = store

    def get(self, analysis_id):
        with self.store.connection() as db:
            return redact_values(scope_snapshot(db, analysis_id))

    def observe(self, scope_id, key, version, *, kind, passed, note, observation,
                personally_verified, repair_id=None, repair_patch_digest=None):
        if not personally_verified or not note.strip() or not observation.strip():
            raise Conflict("scope_observation_required")
        if kind not in ("regression", "integration", "client"):
            raise Conflict("scope_evidence_invalid")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = db.execute("SELECT * FROM task_scopes WHERE id=?", (scope_id,)).fetchone()
            if not scope:
                raise Missing("Task scope not found.")
            if scope["version"] != version or scope["status"] != "reviewed":
                raise Conflict("scope_changed")
            if not scope_snapshot(db, scope["analysis_id"])["reviewed"]:
                raise Conflict("scope_changed")
            if key not in {item["key"] for item in _requirements(db, scope)}:
                raise Conflict("scope_mapping_invalid")
            patch_digest = None
            if repair_id:
                task = db.execute("SELECT * FROM repair_tasks WHERE id=?", (repair_id,)).fetchone()
                if (not task or task["analysis_id"] != scope["analysis_id"] or not task["patch"]
                        or task["revision"] != scope["revision"]
                        or json.loads(task["context"]).get("task_scope", {}).get("version")
                        != version):
                    raise Conflict("scope_evidence_invalid")
                patch_digest = digest(json.loads(task["patch"]))
                if repair_patch_digest != patch_digest:
                    raise Conflict("scope_evidence_invalid")
            elif repair_patch_digest is not None:
                raise Conflict("scope_evidence_invalid")
            details = redact_values({"note": note.strip(), "observation": observation.strip(),
                                     "repair_id": repair_id, "actor": "owner",
                                     "attestation": "personally_observed"})
            db.execute("INSERT INTO task_scope_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                uid("scope_evidence"), scope_id, key, version, scope["revision"], kind,
                "owner", uid("observation"), patch_digest, int(passed),
                json.dumps(details, ensure_ascii=False), time.time(),
            ))
            _event(db, scope, "owner_observation", "owner", {"key": key, "passed": passed})
        return self.get(scope["analysis_id"])

    def add_owner_requirements(self, analysis_id, version, requirements):
        """Owner can append missed requirements; cannot remove or lower a frozen one."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            analysis = db.execute("SELECT * FROM repository_analyses WHERE id=?",
                                  (analysis_id,)).fetchone()
            scope = _bound(db, analysis_id)
            if scope["version"] != version or scope["analysis_id"] != analysis_id:
                raise Conflict("scope_changed")
            # Append uses the same path validation and immutable-requirement contract.
            register_scope(db, analysis, requirements, "owner")
            _event(db, _bound(db, analysis_id), "owner_scope_extension", "owner", {})
        return self.get(analysis_id)

    def record_repair(self, db, task):
        scope = _bound(db, task["analysis_id"])
        review = json.loads(task["review"] or "{}")
        context = json.loads(task["context"])
        verification = json.loads(task["verification"] or "{}")
        patch = json.loads(task["patch"] or "null")
        if (scope["status"] != "reviewed" or scope["analysis_id"] != task["analysis_id"]
                or context.get("task_scope", {}).get("version") != scope["version"]
                or review.get("scope_version") != scope["version"]
                or review.get("approved") is not True or verification.get("passed") is not True
                or not patch):
            return
        stages = ("original_suite", "regression_before", "regression_after", "full_suite_after")
        if any(not isinstance(verification.get(stage), dict) for stage in stages):
            return
        before = verification["regression_before"].get("exit_code")
        if (type(before) is not int or before <= 0 or any(
            type(verification[stage].get("exit_code")) is not int
            or verification[stage]["exit_code"] != 0
            for stage in ("original_suite", "regression_after", "full_suite_after")
        )):
            return
        if (skipped_tests(verification["regression_after"].get("log"))
                or skipped_tests(verification["full_suite_after"].get("log"))
                > skipped_tests(verification["original_suite"].get("log"))):
            return
        requirements = {item["key"]: item for item in _requirements(db, scope)}
        coverage = review.get("scope_coverage", [])
        keys = [item["key"] for item in coverage]
        if len(keys) != len(set(keys)) or any(key not in requirements for key in keys):
            raise Conflict("repair_scope_evidence_invalid")
        for entry in coverage:
            if not set(entry["regression_paths"]) <= set(patch["regression_paths"]):
                raise Conflict("repair_scope_evidence_invalid")
            # Verification is real; relevance to a requirement is the independent review's job.
            ref = digest({"repair": task["id"], "patch": patch, "key": entry["key"],
                          "scope_version": scope["version"], "review": review,
                          "verification": verification})
            if db.execute("SELECT 1 FROM task_scope_evidence WHERE id=?", (ref,)).fetchone():
                continue
            db.execute("INSERT INTO task_scope_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                ref, scope["id"], entry["key"], scope["version"], task["revision"],
                "regression", "repair", task["id"], digest(patch), 1,
                json.dumps({"rationale": entry["rationale"],
                            "regression_paths": entry["regression_paths"],
                            "review_digest": digest(review),
                            "verification_digest": digest(verification),
                            "reviewer": "independent_reviewer", "stages": list(stages)},
                           ensure_ascii=False), time.time(),
            ))

    def publication(self, task, patch):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = _bound(db, task["analysis_id"])
            if not task["patch"] or digest(json.loads(task["patch"])) != digest(patch):
                raise Conflict("repair_scope_changed")
            previous = db.execute("SELECT * FROM task_scope_publications WHERE repair_id=?",
                                  (task["id"],)).fetchone()
            if previous:
                if (previous["patch_digest"] != digest(patch)
                        or previous["revision"] != task["revision"]
                        or previous["scope_id"] != scope["id"]):
                    raise Conflict("repair_scope_changed")
                if previous["mode"] == "closes" and (
                    previous["version"] != scope["version"]
                    or not scope_snapshot(db, task["analysis_id"], candidate=task["id"])["complete"]
                ):
                    raise Conflict("repair_scope_changed")
                return json.loads(previous["snapshot"])
            snapshot = scope_snapshot(db, task["analysis_id"], candidate=task["id"])
            mode = "closes" if snapshot["complete"] else "refs"
            decision = {"scope_id": scope["id"], "version": scope["version"], "mode": mode,
                        "original_digest": scope["original_digest"],
                        "total": snapshot["total"], "done": snapshot["done"],
                        "reviewed": snapshot["reviewed"],
                        "combination_pending": snapshot["combination_pending"],
                        "pending_limits": snapshot["pending_limits"],
                        "remaining": [{"title": item["title"], "status": item["status"],
                                       "missing_evidence": item["missing_evidence"]}
                                      for item in snapshot["items"]
                                      if item["status"] not in COMPLETE]}
            db.execute("INSERT INTO task_scope_publications VALUES(?,?,?,?,?,?,?,?)", (
                task["id"], scope["id"], scope["version"], task["revision"], digest(patch),
                mode, json.dumps(decision, ensure_ascii=False), time.time(),
            ))
            return decision

    def validate_publication(self, task, patch, decision, remote_issue=None):
        current = self.publication(task, patch)
        if current != decision:
            raise Conflict("repair_scope_changed")
        if remote_issue is None:
            return
        with self.store.connection() as db:
            scope = _bound(db, task["analysis_id"])
            original = json.loads(scope["original"])
        fresh = {**original, "title": redact(remote_issue.get("title") or "")[:300],
                 "body": redact(remote_issue.get("body") or "")[:10000],
                 "updated_at": remote_issue.get("updated_at"),
                 "clipped": len(remote_issue.get("body") or "") >= 10000}
        if digest(fresh) != decision["original_digest"]:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                live = _bound(db, task["analysis_id"])
                if live["original_digest"] == decision["original_digest"]:
                    db.execute(
                        "UPDATE task_scopes SET status='draft',version=version+1,original=?,"
                        "original_digest=?,updated=? WHERE id=?",
                        (json.dumps(fresh, ensure_ascii=False), digest(fresh), time.time(),
                         live["id"]),
                    )
                    db.execute(
                        "UPDATE github_issues SET title=?,body=?,updated_at=?,fetched_at=?,"
                        "version=version+1 WHERE id=? AND repo_id=?",
                        (fresh["title"], fresh["body"], fresh["updated_at"], time.time(),
                         original["id"], live["repo_id"]),
                    )
                    _event(db, _bound(db, task["analysis_id"]), "original_changed",
                           "github_read", {"original_digest": digest(fresh)})
            if decision["mode"] == "closes":
                raise Conflict("repair_scope_changed")


def strip_closing_references(text):
    """Only the publisher may generate an automatic Issue closing directive."""
    # Remove keywords regardless of Markdown/link formatting around the reference.
    # Matching only a plain '#123' would miss formatted closing references.
    return re.sub(
        r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b",
        "关联", text, flags=re.IGNORECASE,
    )


def publication_body(body, decision):
    """Untrusted text cannot smuggle a closing directive into partial PRs or commits."""
    lines = ["## 任务范围验收", "",
             f"已核验 {decision['done']} / {decision['total']} 项；"
             + ("整体必需范围已通过当前补丁核验。" if decision["mode"] == "closes" else
                "本 PR 为部分修复，不代表原问题整体完成。")]
    if not decision["reviewed"]:
        lines.append("原始任务范围尚未完成独立审查，完整性未核验。")
    if decision.get("combination_pending"):
        lines.append("各项来自不同候选补丁，组合验收尚未完成。")
    lines.extend("- 待核对验证排除项：" + item["key"]
                 for item in decision.get("pending_limits", []))
    lines.extend("- 待续办：" + item["title"] + "（" + item["status"] + "）"
                 for item in decision["remaining"])
    return strip_closing_references(body + "\n\n" + "\n".join(lines))
