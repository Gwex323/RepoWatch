"""Persistent acceptance jobs and evidence identities; no model or runtime calls."""

import json

from .task_scopes import digest


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS scope_acceptance_runs (
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES task_scopes(id),
            version INTEGER NOT NULL, analysis_id TEXT NOT NULL,
            basis TEXT NOT NULL, basis_hash TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'scope_review',
            owner TEXT, lease REAL NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
            deadline REAL, model_identity TEXT, model_calls INTEGER NOT NULL DEFAULT 0,
            plan TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
            UNIQUE(scope_id,version,basis_hash)
        );
        CREATE TABLE IF NOT EXISTS scope_acceptance_checks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES scope_acceptance_runs(id),
            item_key TEXT NOT NULL, kind TEXT NOT NULL, repair_id TEXT, patch_digest TEXT,
            state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0, specification TEXT NOT NULL,
            result TEXT, review TEXT, created REAL NOT NULL, updated REAL NOT NULL,
            UNIQUE(run_id,item_key,kind)
        );
        CREATE TABLE IF NOT EXISTS scope_repair_revalidations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES scope_acceptance_runs(id),
            scope_id TEXT NOT NULL, version INTEGER NOT NULL, repair_id TEXT NOT NULL,
            repair_basis TEXT NOT NULL, coverage TEXT NOT NULL, created REAL NOT NULL,
            UNIQUE(run_id,repair_id)
        );
        CREATE TABLE IF NOT EXISTS scope_acceptance_followups (
            run_id TEXT PRIMARY KEY REFERENCES scope_acceptance_runs(id),
            root_finding_id TEXT NOT NULL, finding_id TEXT NOT NULL, repair_id TEXT NOT NULL,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scope_acceptance_failures (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES scope_acceptance_runs(id),
            code TEXT NOT NULL, details TEXT NOT NULL, retryable INTEGER NOT NULL,
            created REAL NOT NULL
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(scope_acceptance_runs)")}
    if "retry_after" not in columns:
        db.execute(
            "ALTER TABLE scope_acceptance_runs ADD COLUMN retry_after REAL NOT NULL DEFAULT 0"
        )


def repair_basis(task):
    return digest(
        {
            key: task[key]
            for key in (
                "id",
                "analysis_id",
                "revision",
                "source_digest",
                "context",
                "patch",
                "verification",
                "review",
            )
        }
    )


def revalidation_valid(db, scope, task, details):
    entry = db.execute(
        "SELECT * FROM scope_repair_revalidations WHERE id=?", (details.get("revalidation_id"),)
    ).fetchone()
    if not entry:
        return False
    run = db.execute(
        "SELECT * FROM scope_acceptance_runs WHERE id=?", (entry["run_id"],)
    ).fetchone()
    coverage = json.loads(entry["coverage"])
    plan = json.loads(run["plan"]) if run and run["plan"] else {}
    recorded = next(
        (item["items"] for item in plan.get("coverage", []) if item["repair_id"] == task["id"]),
        None,
    )
    return bool(
        run
        and run["state"] != "stale"
        and run["plan"]
        and entry["scope_id"] == scope["id"]
        and entry["version"] == scope["version"]
        and run["analysis_id"] == scope["analysis_id"]
        and entry["repair_id"] == task["id"]
        and entry["repair_basis"] == repair_basis(task)
        and details.get("coverage_digest") == digest(coverage)
        and recorded == coverage
        and plan.get("scope_review", {}).get("version") == scope["version"]
        and plan.get("scope_review", {}).get("accepted") is True
    )


def check_proof(db, scope, evidence):
    check = db.execute(
        "SELECT * FROM scope_acceptance_checks WHERE id=?", (evidence["reference"],)
    ).fetchone()
    if not check or check["state"] not in ("passed", "failed"):
        return False
    run = db.execute(
        "SELECT * FROM scope_acceptance_runs WHERE id=?", (check["run_id"],)
    ).fetchone()
    if (
        not run
        or run["scope_id"] != scope["id"]
        or run["version"] != scope["version"]
        or run["analysis_id"] != scope["analysis_id"]
        or run["state"] == "stale"
    ):
        return False
    details = json.loads(evidence["details"])
    result = json.loads(check["result"] or "{}")
    review = json.loads(check["review"] or "{}")
    passed = bool(
        check["state"] == "passed"
        and result.get("executed") is True
        and result.get("setup_exit_code") == 0
        and result.get("exit_code") == 0
        and review.get("met") is True
    )
    if (
        check["item_key"] != evidence["item_key"]
        or check["kind"] != evidence["kind"]
        or check["patch_digest"] != evidence["patch_digest"]
        or details.get("result_digest") != digest(json.loads(check["result"] or "{}"))
        or details.get("review_digest") != digest(json.loads(check["review"] or "{}"))
        or bool(evidence["passed"]) != passed
    ):
        return False
    if check["repair_id"]:
        task = db.execute("SELECT * FROM repair_tasks WHERE id=?", (check["repair_id"],)).fetchone()
        return bool(
            task
            and task["patch"]
            and task["revision"] == scope["revision"]
            and check["patch_digest"] == digest(json.loads(task["patch"]))
            and json.loads(run["basis"])["repairs"].get(task["id"]) == repair_basis(task)
        )
    return evidence["patch_digest"] is None


def progress(db, scope):
    run = db.execute(
        "SELECT * FROM scope_acceptance_runs WHERE scope_id=? AND version=? "
        "ORDER BY created DESC LIMIT 1",
        (scope["id"], scope["version"]),
    ).fetchone()
    if not run:
        return None
    failure = db.execute(
        "SELECT code,details,retryable,created FROM scope_acceptance_failures "
        "WHERE run_id=? ORDER BY created DESC,rowid DESC LIMIT 1", (run["id"],),
    ).fetchone()
    return {key: run[key] for key in (
        "id", "state", "stage", "attempt", "error", "updated", "retry_after", "model_calls",
    )} | {
        "failure": ({"code": failure["code"], "details": json.loads(failure["details"]),
                     "retryable": bool(failure["retryable"]), "created": failure["created"]}
                    if failure else None),
        "checks": [
            {
                key: row[key]
                for key in (
                    "item_key",
                    "kind",
                    "state",
                    "reason",
                    "note",
                    "attempt",
                )
            }
            for row in db.execute(
                "SELECT * FROM scope_acceptance_checks WHERE run_id=? ORDER BY created,id",
                (run["id"],),
            )
        ],
    }
