"""Durable repair queue, evidence gates, SDK handoff and publication."""

import asyncio
import hashlib
import json
import logging
import re
import threading
import time

from .diagnosis import redact, redact_values
from .environment_recovery import EnvironmentRecovery
from .github_accounts import GitHubError
from .github_contributions import GitHubPublisher
from .repair_model import (
    FAILURE_POLICY,
    OUTPUT_POLICY,
    PROTOCOL_POLICY,
    REVIEW_EVIDENCE_POLICY,
    VERIFICATION_POLICY,
    RepairReview,
    repair_failure_code,
    repair_failure_details,
    verification_feedback,
)
from .repair_origin import analysis_origin, repair_origin
from .repair_workspace import SOURCE_READ_POLICY, Patch, RepairWorkspace, source_tree
from .store import Conflict, Missing, uid
from .task_scopes import TaskScopes, digest, scope_packet, scope_snapshot

REPAIR_TIMEOUT_SECONDS = 1800
REPAIR_MAX_ATTEMPTS = 3
PUBLICATION_MAX_RETRIES = 3
PUBLICATION_RETRY_ERRORS = {
    "github_invalid_token", "github_permission_denied", "github_forbidden",
    "github_rate_limited", "github_denied_or_rate_limited", "github_unreachable",
    "github_unavailable", "github_write_rejected", "github_repository_unavailable",
    "repair_publication_uncertain", "repair_fork_pending", "repair_account_changed",
    "repair_interrupted", "repair_paused", "repair_deadline_exceeded",
}
REPAIR_REQUEST_POLICY = "extended_timeout_v1"
WORKSPACE_POLICY = "preflight_retained_patch_paths_v1"


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS repair_tasks (
            id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES repository_analyses(id),
            finding_id TEXT NOT NULL REFERENCES analysis_findings(id),
            env_id TEXT NOT NULL REFERENCES environments(id),
            repo_id TEXT NOT NULL REFERENCES repositories(id), revision TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', stage TEXT, attempt INTEGER NOT NULL DEFAULT 0,
            lease REAL NOT NULL DEFAULT 0, context TEXT NOT NULL, patch TEXT, diff TEXT,
            verification TEXT, review TEXT, remote TEXT, error TEXT,
            owner TEXT, source_digest TEXT, version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(finding_id)
        );
        CREATE TABLE IF NOT EXISTS repair_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_id TEXT NOT NULL REFERENCES repair_tasks(id),
            kind TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repair_remote_operations (
            repair_id TEXT NOT NULL REFERENCES repair_tasks(id), operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
            PRIMARY KEY(repair_id, operation)
        );
        CREATE TABLE IF NOT EXISTS repair_attempts (
            repair_id TEXT NOT NULL REFERENCES repair_tasks(id), attempt INTEGER NOT NULL,
            snapshot TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(repair_id, attempt)
        );
        CREATE TABLE IF NOT EXISTS repair_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1
        );
        INSERT OR IGNORE INTO repair_settings(id) VALUES(1);
    """)


class Repairs(EnvironmentRecovery):
    def __init__(self, execution, engine=None, github=None, *, analyses=None, publisher=None):
        from .repository_analysis import Analyses

        self.execution, self.store = execution, execution.store
        self.engine = engine
        self.publisher = publisher or GitHubPublisher(github or execution.github)
        self.analyses = analyses or Analyses(execution, engine)
        self.owner, self.closing = uid("repair_worker"), threading.Event()
        self.scopes = TaskScopes(self.store)

    def settings(self):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM repair_settings WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "version": row["version"],
                "model_enabled": bool(self.engine and self.engine.status()["enabled"]),
                "max_attempts": REPAIR_MAX_ATTEMPTS, "timeout_seconds": REPAIR_TIMEOUT_SECONDS}

    def configure(self, version, enabled):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE repair_settings SET enabled=?,version=version+1 WHERE id=1 AND version=?",
                (int(enabled), version),
            ).rowcount
            if not changed:
                raise Conflict("repair_settings_changed")
        return self.settings()

    def event(self, repair_id, kind, value=""):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,?)",
                (repair_id, kind, str(redact_values(str(value)))[:12000], time.time()),
            )

    @staticmethod
    def _json(row, key):
        return json.loads(row[key]) if row[key] else None

    def get(self, repair_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (repair_id,)).fetchone()
            if not row:
                raise Missing("Repair task not found.")
            result = {key: row[key] for key in (
                "id", "analysis_id", "finding_id", "env_id", "repo_id", "revision",
                "status", "stage", "attempt", "version", "error", "created", "updated",
            )}
            for key in ("context", "patch", "verification", "review", "remote"):
                result[key] = self._json(row, key)
            finding = db.execute("SELECT status,feedback FROM analysis_findings WHERE id=?",
                                 (row["finding_id"],)).fetchone()
            result["finding_status"] = finding["status"] if finding else None
            result["finding_feedback"] = (json.loads(finding["feedback"])
                                          if finding and finding["feedback"] else None)
            result["patch_digest"] = digest(result["patch"]) if result["patch"] else None
            result["task_scope"] = scope_snapshot(db, row["analysis_id"])
            try:
                result.update(repair_origin(db, row))
                result["source_error"] = None
            except Conflict as exc:
                result.update(source_kind=None, source_issue=None, source_error=str(exc))
            result["verification_rounds"] = (
                result["verification"].pop("_rounds", []) if result["verification"] else []
            )
            result["diff"] = row["diff"] or ""
            result["events"] = [dict(item) for item in db.execute(
                "SELECT kind,value,created FROM repair_events WHERE repair_id=? ORDER BY id",
                (repair_id,),
            )]
            result["attempts"] = [json.loads(item[0]) for item in db.execute(
                "SELECT snapshot FROM repair_attempts WHERE repair_id=? ORDER BY attempt",
                (repair_id,),
            )]
            result["enabled"] = bool(db.execute(
                "SELECT enabled FROM repair_settings WHERE id=1"
            ).fetchone()[0])
            result["timeout_recovery_available"] = self._timeout_recovery(row)
            result["source_read_recovery_available"] = self._source_read_recovery(row)
            result["failure_recovery_available"] = self._failure_recovery(row)
            result["output_recovery_available"] = self._output_recovery(db, row)
            result["protocol_recovery_available"] = self._protocol_recovery(db, row)
            result["workspace_recovery_available"] = self._workspace_recovery(db, row)
            result["verification_recovery_available"] = self._verification_recovery(row)
            result["review_evidence_recovery_available"] = self._review_evidence_recovery(db, row)
            result["verification_resume_available"] = (
                self._can_resume_verification(row) or result["review_evidence_recovery_available"]
            )
            result["docker_recovery_available"] = self._docker_recovery(db, row)
            publication_failure = self._publication_failure(row)
            result["publication_retry_available"] = (
                self._publication_retry_available(row) and self.current(db, row)
            )
            result["can_retry"] = (
                row["status"] in ("failed", "interrupted", "waiting_environment")
                and (not publication_failure or result["publication_retry_available"])
                and (result["publication_retry_available"] or result["docker_recovery_available"]
                     or row["attempt"] < self._attempt_limit(row) or self._timeout_recovery(row)
                     or self._source_read_recovery(row) or self._failure_recovery(row)
                     or result["output_recovery_available"]
                     or result["protocol_recovery_available"]
                     or result["workspace_recovery_available"]
                     or result["verification_recovery_available"]
                     or result["review_evidence_recovery_available"])
                and self.current(db, row)
            )
            if result["source_error"] or row["error"] in (
                "repair_issue_publication_conflict", "repair_scope_changed",
                "repair_scope_evidence_invalid",
            ):
                result["can_retry"] = False
            return redact_values(result)

    @classmethod
    def _publication_failure(cls, row):
        return (
            row["status"] in ("failed", "interrupted") and row["stage"] == "publishing"
            and row["error"] in PUBLICATION_RETRY_ERRORS and cls._verified(row)
        )

    @classmethod
    def _publication_retry_available(cls, row):
        used = json.loads(row["context"]).get("publication_retries", 0)
        return (cls._publication_failure(row) and type(used) is int
                and 0 <= used < PUBLICATION_MAX_RETRIES)

    @staticmethod
    def _attempt_limit(row):
        context = json.loads(row["context"])
        return (REPAIR_MAX_ATTEMPTS + int(context.get("timeout_recovery_used") is True)
                + int(context.get("source_read_recovery_used") is True)
                + int(context.get("failure_recovery_used") is True)
                + int(context.get("output_recovery_used") is True)
                + int(context.get("protocol_recovery_used") is True)
                + int(context.get("workspace_recovery_used") is True)
                + int(context.get("verification_recovery_used") is True)
                + int(context.get("review_evidence_recovery_used") is True)
                + int(context.get("environment_recovery_used") is True))

    @classmethod
    def _verification_recovery(cls, row):
        context = json.loads(row["context"])
        verification = cls._json(row, "verification")
        return bool(
            row["status"] == "failed" and row["error"] == "repair_tests_failed"
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("verification_policy") != VERIFICATION_POLICY
            and not context.get("verification_recovery_used")
            and row["patch"] and row["source_digest"] and not row["review"] and not row["remote"]
            and isinstance(verification, dict) and verification.get("passed") is False
            and len(verification_feedback(verification)) == 4
        )
    @classmethod
    def _review_evidence_recovery(cls, db, row):
        context = json.loads(row["context"])
        verification = cls._json(row, "verification")
        if not (
            row["status"] == "failed" and row["error"] == "repair_handoff_incomplete"
            and row["stage"] == "independent_reviewer"
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("review_evidence_policy") != REVIEW_EVIDENCE_POLICY
            and not context.get("review_evidence_recovery_used")
            and row["patch"] and row["diff"] and row["source_digest"]
            and isinstance(verification, dict) and verification.get("passed") is True
            and not row["review"] and not row["remote"]
        ):
            return False
        handed_off = boundary = False
        for event in db.execute(
            "SELECT kind,value FROM repair_events WHERE repair_id=? ORDER BY id DESC",
            (row["id"],),
        ):
            if event["kind"] in ("retry", "queued"):
                boundary = event["kind"] == "retry" and event["value"] == str(row["attempt"])
                break
            if event["kind"] == "tool_call" and event["value"] == "read_repair_verification":
                return False
            if event["kind"] == "handoff":
                if event["value"] != "repair_agent -> independent_reviewer" or handed_off:
                    return False
                handed_off = True
        return handed_off and boundary

    @staticmethod
    def _timeout_recovery(row):
        context = json.loads(row["context"])
        return (
            row["status"] == "failed" and row["error"] == "repair_model_timeout"
            and row["attempt"] == REPAIR_MAX_ATTEMPTS
            and context.get("request_policy") != REPAIR_REQUEST_POLICY
            and not context.get("timeout_recovery_used")
        )

    @classmethod
    def _source_read_recovery(cls, row):
        context = json.loads(row["context"])
        return (
            row["status"] == "failed" and row["error"] == "repair_file_unavailable"
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("source_read_policy") != SOURCE_READ_POLICY
            and not context.get("source_read_recovery_used")
        )

    @classmethod
    def _failure_recovery(cls, row):
        context = json.loads(row["context"])
        return (
            row["status"] == "failed" and row["error"] == "repair_failed"
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("failure_policy") != FAILURE_POLICY
            and not context.get("failure_recovery_used")
            and not row["patch"] and not row["verification"] and not row["remote"]
        )

    @classmethod
    def _output_recovery(cls, db, row):
        context = json.loads(row["context"])
        if not (
            row["status"] == "failed"
            and row["error"] in {"repair_output_invalid", "repair_output_truncated"}
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("output_policy") != OUTPUT_POLICY
            and not context.get("output_recovery_used")
            and not row["patch"] and not row["verification"]
            and not row["review"] and not row["remote"]
        ):
            return False
        # Only the latest attempt's trusted provider metadata qualifies an old
        # format/truncation failure; malformed JSON alone must not gain an attempt.
        truncated = 0
        for event in db.execute(
            "SELECT kind,value FROM repair_events WHERE repair_id=? "
            "AND kind IN ('retry','provider_response') ORDER BY id DESC", (row["id"],),
        ):
            if event["kind"] == "retry":
                break
            try:
                details = json.loads(event["value"])
            except ValueError:
                return False
            if not isinstance(details, dict) or details.get("truncated") is not True:
                return False
            truncated += 1
            if truncated >= 2:
                return True
        return False

    @classmethod
    def _protocol_recovery(cls, db, row):
        context = json.loads(row["context"])
        if not (
            row["status"] == "failed" and row["error"] == "repair_tests_failed"
            and row["attempt"] == cls._attempt_limit(row)
            and context.get("protocol_policy") != PROTOCOL_POLICY
            and not context.get("protocol_recovery_used")
            and not row["patch"] and not row["verification"] and not row["remote"]
        ):
            return False
        invalid = 0
        for event in db.execute(
            "SELECT kind,value FROM repair_events WHERE repair_id=? ORDER BY id DESC",
            (row["id"],),
        ):
            if event["kind"] == "retry":
                break
            if event["kind"] in ("patch", "handoff", "testing"):
                return False
            if event["kind"] == "tool_arguments_invalid":
                try:
                    details = json.loads(event["value"])
                except ValueError:
                    return False
                if (isinstance(details, dict)
                        and details.get("tool") == "submit_and_test_repair"
                        and details.get("fields") == ["patch"]
                        and details.get("reason") == "arguments_schema"):
                    invalid += 1
        return invalid >= 2

    @classmethod
    def _workspace_recovery(cls, db, row):
        context = json.loads(row["context"])
        if not (
            row["status"] == "failed" and row["attempt"] == cls._attempt_limit(row)
            and context.get("workspace_policy") != WORKSPACE_POLICY
            and not context.get("workspace_recovery_used") and not row["remote"]
            and not cls._json(row, "verification") and not cls._json(row, "review")
        ):
            return False
        if row["error"] == "docker_unavailable":
            return cls._can_resume_verification(row)
        if row["error"] != "repair_unsafe_path" or row["patch"]:
            return False
        for event in db.execute(
            "SELECT kind,value FROM repair_events WHERE repair_id=? ORDER BY id DESC",
            (row["id"],),
        ):
            if event["kind"] in ("retry", "patch", "testing", "handoff"):
                return False
            if event["kind"] == "tool_failure":
                try:
                    details = json.loads(event["value"])
                except ValueError:
                    return False
                return (isinstance(details, dict)
                        and details.get("tool") == "submit_and_test_repair"
                        and details.get("code") == "repair_unsafe_path")
        return False

    @classmethod
    def _can_resume_verification(cls, row):
        return bool(
            row["error"] == "docker_unavailable" and row["patch"] and row["source_digest"]
            and not cls._json(row, "verification") and not cls._json(row, "review")
            and not row["remote"]
        )

    def list(self, env_id):
        with self.store.connection() as db:
            ids = [row["id"] for row in db.execute(
                "SELECT id FROM repair_tasks WHERE env_id=? ORDER BY created DESC LIMIT 20",
                (env_id,),
            )]
        return [self.get(item) for item in ids]

    def current(self, db, task):
        env = db.execute("SELECT * FROM environments WHERE id=?", (task["env_id"],)).fetchone()
        analysis = db.execute(
            "SELECT * FROM repository_analyses WHERE id=?", (task["analysis_id"],)
        ).fetchone()
        finding = db.execute(
            "SELECT * FROM analysis_findings WHERE id=?", (task["finding_id"],)
        ).fetchone()
        if (not env or not analysis or not finding or analysis["status"] != "completed"
                or finding["analysis_id"] != analysis["id"]):
            return False
        if env["revision"] != task["revision"] or analysis["revision"] != task["revision"]:
            return False
        feedback = json.loads(finding["feedback"] or "{}")
        return (
            finding["status"] == "confirmed" and feedback.get("decision") == "confirmed"
            and feedback.get("actor") == "owner" and feedback.get("revision") == task["revision"]
            and self.analyses.current(analysis)
            and not db.execute(
                "SELECT 1 FROM execution_jobs WHERE env_id=? AND state IN ('queued','running')",
                (task["env_id"],),
            ).fetchone()
        )

    def enqueue(self, analysis_id, finding_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.enqueue_in(db, analysis_id, finding_id)

    def enqueue_in(self, db, analysis_id, finding_id):
        analysis = db.execute(
            "SELECT * FROM repository_analyses WHERE id=?", (analysis_id,)
        ).fetchone()
        finding = db.execute(
            "SELECT * FROM analysis_findings WHERE id=? AND analysis_id=?",
            (finding_id, analysis_id),
        ).fetchone()
        if not analysis or not finding:
            raise Missing("Finding not found.")
        bound = {"analysis_id": analysis_id, "finding_id": finding_id,
                 "env_id": analysis["env_id"], "revision": analysis["revision"]}
        if not self.current(db, bound):
            raise Conflict("repair_confirmation_required")
        old = db.execute("SELECT id FROM repair_tasks WHERE finding_id=?", (finding_id,)).fetchone()
        if old:
            return old["id"]
        env = db.execute("SELECT * FROM environments WHERE id=?", (analysis["env_id"],)).fetchone()
        context = {
            "finding": json.loads(finding["data"]), "confirmation": json.loads(finding["feedback"]),
            "revision": analysis["revision"], "repo_id": analysis["repo_id"],
            "env_id": analysis["env_id"], "config": json.loads(env["config"] or "{}"),
            "memories": json.loads(analysis["context"] or "{}").get("memories", []),
            **analysis_origin(db, analysis),
            "task_scope": scope_packet(db, analysis_id),
        }
        task_id, now = uid("repair"), time.time()
        db.execute(
            "INSERT INTO repair_tasks(id,analysis_id,finding_id,env_id,repo_id,revision,"
            "context,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, analysis_id, finding_id, analysis["env_id"], analysis["repo_id"],
             analysis["revision"], json.dumps(context, ensure_ascii=False), now, now),
        )
        db.execute(
            "INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,?)",
            (task_id, "queued", "confirmed_finding", now),
        )
        return task_id

    def backfill(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT a.id AS analysis_id,f.id AS finding_id FROM repository_analyses a "
                "JOIN analysis_findings f ON f.analysis_id=a.id "
                "WHERE a.status='completed' AND f.status='confirmed'"
            ).fetchall()
        for row in rows:
            try:
                self.enqueue(row["analysis_id"], row["finding_id"])
            except (Conflict, Missing):
                pass

    def cancel_pending_in(self, db, finding_id, *, decision=None):
        row = db.execute("SELECT * FROM repair_tasks WHERE finding_id=?", (finding_id,)).fetchone()
        if not row or row["status"] not in (
            "queued", "failed", "interrupted", "waiting_environment",
        ):
            return
        # Preserve the last failed attempt as history before closing its active task.
        if row["status"] != "queued":
            snapshot = {key: row[key] for key in ("attempt", "stage", "error", "diff")}
            snapshot.update({key: self._json(row, key)
                             for key in ("patch", "verification", "review", "remote")})
            db.execute(
                "INSERT OR IGNORE INTO repair_attempts(repair_id,attempt,snapshot,created) "
                "VALUES(?,?,?,?)", (row["id"], row["attempt"], json.dumps(snapshot), time.time()),
            )
        db.execute(
            "UPDATE repair_tasks SET status='cancelled',stage=?,error=NULL,lease=0,"
            "version=version+1,updated=? WHERE id=?",
            ("false_positive" if decision == "false_positive" else "cancelled",
             time.time(), row["id"]),
        )
        db.execute(
            "INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,?)",
            (row["id"], "cancelled", decision or "finding_disposition", time.time()),
        )

    def resolution_verification(self, finding_id):
        """Return merged-PR evidence for a published fix, or None for owner assertion."""
        with self.store.connection() as db:
            repair = db.execute("SELECT * FROM repair_tasks WHERE finding_id=?",
                                (finding_id,)).fetchone()
        if not repair or repair["status"] != "pr_created" or not repair["remote"]:
            return None
        remote = json.loads(repair["remote"])
        pr = remote.get("pr") if isinstance(remote, dict) else None
        url = pr.get("url") if isinstance(pr, dict) else None
        account_id = remote.get("account_id")
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)", url or "",
        )
        if not match or type(account_id) is not int:
            raise Conflict("resolution_verification_failed")
        github = self.publisher.github
        try:
            token, _ = github.validated_identity(account_id, None)
            result, _ = github.client.request_json(
                "GET", f"/repos/{match.group(1)}/pulls/{match.group(2)}", token,
            )
        except (Conflict, Missing, GitHubError):
            raise Conflict("resolution_verification_failed") from None
        if (not isinstance(result, dict) or result.get("merged") is not True
                or result.get("html_url") != url):
            raise Conflict("resolution_pr_not_merged")
        return {"pr": url}

    def retry(self, repair_id, version):
        with self.store.connection() as db:
            previous = db.execute("SELECT * FROM repair_tasks WHERE id=?", (repair_id,)).fetchone()
        if (previous and previous["version"] == version
                and previous["error"] == "docker_unavailable"):
            ready = self.execution.runtime.readiness()
            if not ready["ready"]:
                raise Conflict(ready["reason"])
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (repair_id,)).fetchone()
            if not row:
                raise Missing("Repair task not found.")
            repair_origin(db, row)
            if row["error"] in ("repair_issue_publication_conflict", "repair_scope_changed",
                                "repair_scope_evidence_invalid"):
                raise Conflict(row["error"])
            output_recovery = self._output_recovery(db, row)
            protocol_recovery = self._protocol_recovery(db, row)
            workspace_recovery = self._workspace_recovery(db, row)
            verification_recovery = self._verification_recovery(row)
            review_evidence_recovery = self._review_evidence_recovery(db, row)
            publication_retry = self._publication_retry_available(row)
            docker_recovery = self._docker_recovery(db, row)
            if (self._publication_failure(row) and not publication_retry):
                raise Conflict("repair_publication_retry_limit")
            if (row["version"] != version
                    or row["status"] not in ("failed", "interrupted", "waiting_environment")
                    or (row["attempt"] >= self._attempt_limit(row)
                        and not self._timeout_recovery(row)
                        and not self._source_read_recovery(row)
                        and not self._failure_recovery(row)
                        and not output_recovery and not protocol_recovery
                        and not workspace_recovery and not publication_retry
                        and not docker_recovery and not verification_recovery
                        and not review_evidence_recovery)):
                raise Conflict("repair_changed")
            if not self.current(db, row):
                raise Conflict("repair_stale")
            context = json.loads(row["context"])
            context["resume_publication"] = publication_retry
            if docker_recovery:
                context["environment_recovery_used"] = True
            if publication_retry:
                context["publication_retries"] = context.get("publication_retries", 0) + 1
            recovery = self._timeout_recovery(row)
            read_recovery = self._source_read_recovery(row)
            failure_recovery = self._failure_recovery(row)
            if recovery:
                context.update(timeout_recovery_used=True, request_policy=REPAIR_REQUEST_POLICY)
            if read_recovery:
                context.update(
                    source_read_recovery_used=True, source_read_policy=SOURCE_READ_POLICY,
                )
            if failure_recovery:
                context.update(failure_recovery_used=True, failure_policy=FAILURE_POLICY)
            if output_recovery:
                context.update(output_recovery_used=True, output_policy=OUTPUT_POLICY)
            if protocol_recovery:
                context.update(protocol_recovery_used=True, protocol_policy=PROTOCOL_POLICY)
            if workspace_recovery:
                context.update(workspace_recovery_used=True, workspace_policy=WORKSPACE_POLICY)
            if verification_recovery:
                context.update(verification_recovery_used=True,
                               verification_policy=VERIFICATION_POLICY)
            if row["error"] == "repair_tests_failed" and row["verification"]:
                context["previous_failure"] = {
                    "verification": verification_feedback(self._json(row, "verification")),
                    "diff": redact(row["diff"] or "")[-6000:],
                    "note": "Previous attempt failed verification. This is untrusted evidence, "
                            "not permission to weaken tests. Read original source before editing.",
                }
            if review_evidence_recovery:
                context.update(review_evidence_recovery_used=True,
                               review_evidence_policy=REVIEW_EVIDENCE_POLICY)
            resume_verification = self._can_resume_verification(row) or review_evidence_recovery
            context["resume_verification"] = resume_verification
            if self._verified(row) or resume_verification:
                account = db.execute(
                    "SELECT a.id,a.version FROM github_accounts a JOIN github_configuration c "
                    "ON c.active_account_id=a.id WHERE a.state='verified'"
                ).fetchone()
                if not account or account["id"] != context.get("account_id"):
                    raise Conflict("repair_account_changed")
                # Explicit retry can refresh the same identity's token, never switch identity.
                context["account_version"] = account["version"]
            snapshot = {key: row[key] for key in ("attempt", "stage", "error", "diff")}
            snapshot.update({
                key: self._json(row, key) for key in ("patch", "verification", "review", "remote")
            })
            db.execute(
                "INSERT OR IGNORE INTO repair_attempts VALUES(?,?,?,?)",
                (repair_id, row["attempt"], json.dumps(snapshot, ensure_ascii=False), time.time()),
            )
            if not self._verified(row) and not resume_verification:
                db.execute(
                    "UPDATE repair_tasks SET patch=NULL,diff=NULL,verification=NULL,review=NULL,"
                    "source_digest=NULL WHERE id=?", (repair_id,),
                )
            db.execute(
                "UPDATE repair_tasks SET status='queued',stage=NULL,error=NULL,version=version+1,"
                "context=?,updated=? WHERE id=?",
                (json.dumps(context, ensure_ascii=False), time.time(), repair_id),
            )
        self.event(repair_id, "retry", str(row["attempt"] + 1))
        if review_evidence_recovery:
            self.event(repair_id, "review_evidence_recovery", REVIEW_EVIDENCE_POLICY)
        if docker_recovery:
            self.event(repair_id, "environment_recovery", "docker_ready")
        if publication_retry:
            self.event(repair_id, "publication_retry", str(context["publication_retries"]))
        if recovery:
            self.event(repair_id, "timeout_recovery", REPAIR_REQUEST_POLICY)
        if read_recovery:
            self.event(repair_id, "source_read_recovery", SOURCE_READ_POLICY)
        if failure_recovery:
            self.event(repair_id, "failure_recovery", FAILURE_POLICY)
        if output_recovery:
            self.event(repair_id, "output_recovery", OUTPUT_POLICY)
        if protocol_recovery:
            self.event(repair_id, "protocol_recovery", PROTOCOL_POLICY)
        if workspace_recovery:
            self.event(repair_id, "workspace_recovery", WORKSPACE_POLICY)
        if verification_recovery:
            self.event(repair_id, "verification_recovery", VERIFICATION_POLICY)
        return self.get(repair_id)

    @staticmethod
    def _verified(row):
        return bool(
            row["patch"] and row["review"] and row["verification"]
            and json.loads(row["review"]).get("approved") is True
            and (json.loads(row["verification"]) or {}).get("passed") is True
        )

    def claim(self, *, allow_work=True):
        candidate = self._environment_candidate(allow_work=allow_work)
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT id FROM repair_tasks WHERE status='running' AND lease<?", (now,)
            ).fetchall():
                db.execute(
                    "UPDATE repair_tasks SET status='interrupted',error='repair_interrupted',"
                    "lease=0,version=version+1,updated=? WHERE id=?", (now, row["id"]),
                )
            if (not allow_work or not db.execute(
                "SELECT enabled FROM repair_settings WHERE id=1"
            ).fetchone()[0]):
                return None
            if db.execute("SELECT 1 FROM repair_tasks WHERE status='running'").fetchone():
                return None
            if not candidate:
                return None
            row = db.execute(
                "SELECT * FROM repair_tasks WHERE id=? AND version=? AND status='queued'",
                (candidate["id"], candidate["version"]),
            ).fetchone()
            if not row:
                return None
            context = json.loads(row["context"])
            if candidate.get("preflight_source_digest"):
                context["preflight_source_digest"] = candidate["preflight_source_digest"]
            publication_resume = (
                context.pop("resume_publication", False) is True and self._verified(row)
                and type(context.get("publication_retries")) is int
                and 1 <= context["publication_retries"] <= PUBLICATION_MAX_RETRIES
            )
            if row["attempt"] >= self._attempt_limit(row) and not publication_resume:
                db.execute(
                    "UPDATE repair_tasks SET status='failed',"
                    "error='repair_attempt_limit',updated=? "
                    "WHERE id=?", (now, row["id"]),
                )
                return None
            context["request_policy"] = REPAIR_REQUEST_POLICY
            context["source_read_policy"] = SOURCE_READ_POLICY
            context["failure_policy"] = FAILURE_POLICY
            context["output_policy"] = OUTPUT_POLICY
            context["protocol_policy"] = PROTOCOL_POLICY
            context["workspace_policy"] = WORKSPACE_POLICY
            context["verification_policy"] = VERIFICATION_POLICY
            context["review_evidence_policy"] = REVIEW_EVIDENCE_POLICY
            if not self._verified(row):
                context["task_scope"] = scope_packet(db, row["analysis_id"])
            encoded_context = json.dumps(context, ensure_ascii=False)
            db.execute(
                "UPDATE repair_tasks SET status='running',stage=?,attempt=attempt+1,"
                "owner=?,lease=?,version=version+1,updated=?,context=? WHERE id=?",
                ("publishing" if self._verified(row) else "repair_agent", self.owner,
                 now + REPAIR_TIMEOUT_SECONDS + 30, now, encoded_context, row["id"]),
            )
            return {**dict(row), "context": encoded_context, "owner": self.owner,
                    "attempt": row["attempt"] + 1}

    async def tick(self):
        self.backfill()
        row = await asyncio.to_thread(
            self.claim, allow_work=bool(self.engine and self.engine.status()["enabled"]),
        )
        if not row:
            return False
        started, workspace = time.monotonic(), None
        stop = threading.Event()
        def check():
            if self.closing.is_set() or self.execution.closing.is_set() or stop.is_set():
                raise Conflict("repair_interrupted")
            if time.monotonic() - started > REPAIR_TIMEOUT_SECONDS:
                raise Conflict("repair_deadline_exceeded")
            with self.store.connection() as db:
                settings = db.execute("SELECT enabled FROM repair_settings WHERE id=1").fetchone()
                latest = db.execute(
                    "SELECT * FROM repair_tasks WHERE id=?", (row["id"],)
                ).fetchone()
                if not settings[0]:
                    raise Conflict("repair_paused")
                if not self.engine.status()["enabled"]:
                    raise Conflict("repair_model_required")
                if not latest or latest["owner"] != self.owner or latest["status"] != "running":
                    raise Conflict("repair_changed")
                if not self.current(db, latest):
                    raise Conflict("repair_stale")
        try:
            check()
            context = json.loads(row["context"])
            with self.store.connection() as db:
                context.update(repair_origin(db, row))
            def event(kind, value=""):
                self.event(row["id"], kind, value)
                if kind == "verification_round" and workspace:
                    self._save_artifacts(row["id"], workspace)
                if kind == "verification_feedback":
                    with self.store.connection() as db:
                        db.execute("UPDATE repair_tasks SET stage='repair_agent',updated=? "
                                   "WHERE id=?",
                                   (time.time(), row["id"]))
                if kind in ("agent", "testing"):
                    with self.store.connection() as db:
                        db.execute(
                            "UPDATE repair_tasks SET stage=?,updated=? WHERE id=?",
                            (value, time.time(), row["id"]),
                        )
            if self._verified(row):
                # Resume a verified publication without re-running the model or modifying files.
                await asyncio.to_thread(self.publish, row, check)
                return True
            env = self.execution.get(row["env_id"])
            if not env["config"].get("test", "").strip():
                raise Conflict("repair_test_command_required")
            with self.store.connection() as db:
                account = db.execute(
                    "SELECT a.id,a.version FROM github_accounts a JOIN github_configuration c "
                    "ON c.active_account_id=a.id WHERE a.state='verified'"
                ).fetchone()
            if not account:
                raise Conflict("github_active_account_required")
            if context.get("environment_account_id", account["id"]) != account["id"]:
                raise Conflict("repair_account_changed")
            context.update(account_id=account["id"], account_version=account["version"])
            with self.store.connection() as db:
                db.execute("UPDATE repair_tasks SET context=? WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), row["id"]))
            workspace = await asyncio.to_thread(
                RepairWorkspace, self.execution, row["id"], env, check, attempt=row["attempt"],
            )
            with self.store.connection() as db:
                analysis_context = json.loads(db.execute(
                    "SELECT context FROM repository_analyses WHERE id=?", (row["analysis_id"],)
                ).fetchone()[0] or "{}")
            for path, captured in analysis_context.get("files", {}).items():
                if path not in workspace.files:
                    raise Conflict("repair_source_changed")
                actual = redact(
                    (workspace.original / path).read_bytes()[:16000].decode(
                        "utf-8", errors="replace"
                    )
                )
                if not actual.startswith(captured):
                    raise Conflict("repair_source_changed")
            source_digest = hashlib.sha256(
                json.dumps(workspace.files, sort_keys=True).encode()
            ).hexdigest()
            if context.get("preflight_source_digest", source_digest) != source_digest:
                raise Conflict("repair_source_changed")
            if context.get("resume_verification") and row["source_digest"] != source_digest:
                raise Conflict("repair_source_changed")
            with self.store.connection() as db:
                db.execute("UPDATE repair_tasks SET source_digest=? WHERE id=?",
                           (source_digest, row["id"]))
            if context.get("resume_verification"):
                retained = self._json(row, "patch")
                patch = Patch.model_validate({
                    "summary": retained["summary"],
                    "edits": [{key: edit[key] for key in ("path", "original_sha256", "content")}
                              for edit in retained["edits"]],
                    "regression_paths": retained["regression_paths"],
                    "regression_command": retained["regression_command"],
                })
                for edit in patch.edits:
                    if edit.original_sha256:
                        await asyncio.to_thread(workspace.read, edit.path)
                await asyncio.to_thread(workspace.prepare, patch)
                event("verification_resume", "retained_patch")
                worker = asyncio.create_task(asyncio.to_thread(workspace.verify, event))
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    stop.set()
                    try:
                        await worker
                    except Exception:
                        pass
                    raise
            call = asyncio.create_task(self.engine.repair(
                redact_values(context), event, workspace=workspace,
            ))
            try:
                while not call.done():
                    check()
                    await asyncio.wait({call}, timeout=0.5)
                raw_review = await call
                if raw_review.get("handoffs") != ["repair_agent -> independent_reviewer"]:
                    raise Conflict("repair_handoff_incomplete")
                review = RepairReview.model_validate({
                    key: raw_review[key] for key in RepairReview.model_fields if key in raw_review
                }).model_dump()
            finally:
                if not call.done():
                    stop.set()
                    call.cancel()
                    try:
                        await call
                    except (asyncio.CancelledError, Exception):
                        pass
            check()
            self._save_artifacts(row["id"], workspace, review)
            if not review["approved"]:
                raise Conflict("repair_review_rejected")
            if not workspace.verification or not workspace.verification.get("passed"):
                raise Conflict("repair_tests_failed")
            self.event(row["id"], "review_approved", review["summary"])
            with self.store.connection() as db:
                ready = dict(db.execute(
                    "SELECT * FROM repair_tasks WHERE id=?", (row["id"],)
                ).fetchone())
            await asyncio.to_thread(self.publish, ready, check)
        except Exception as exc:
            code = self._error_code(exc, workspace)
            self.event(row["id"], "failure_details", json.dumps({
                "code": code, **repair_failure_details(exc),
            }))
            if workspace:
                self._save_artifacts(row["id"], workspace)
            with self.store.connection() as db:
                db.execute(
                    "UPDATE repair_tasks SET status=?,error=?,lease=0,version=version+1,updated=? "
                    "WHERE id=? AND owner=?",
                    ("interrupted" if code == "repair_interrupted" else "failed",
                     code[:80], time.time(), row["id"], self.owner),
                )
            self.event(row["id"], "failure", code)
            logging.getLogger(__name__).warning("Repair %s failed: %s", row["id"], code)
        return True

    @staticmethod
    def _error_code(exc, workspace):
        code = repair_failure_code(exc)
        if code != "repair_failed":
            return code
        if workspace and workspace.verification and not workspace.verification.get("passed"):
            setup_failed = any(
                isinstance(value, dict) and key.endswith("_setup")
                and value.get("exit_code") != 0
                for key, value in workspace.verification.items()
            )
            if setup_failed:
                return "repair_setup_failed"
            if "regression_before" in workspace.verification:
                return "repair_tests_failed"
        return code

    def _save_artifacts(self, repair_id, workspace, review=None):
        if workspace.patch is None:
            return
        patch = workspace.patch.model_dump()
        for edit in patch["edits"]:
            original = workspace.original / edit["path"]
            edit["mode"] = (
                "100755" if original.exists() and original.stat().st_mode & 0o111 else "100644"
            )
            if edit["original_sha256"]:
                raw = original.read_bytes()
                edit["original_git_blob"] = hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest()
        with self.store.connection() as db:
            db.execute(
                "UPDATE repair_tasks SET patch=?,diff=?,verification=?,"
                "review=COALESCE(?,review),updated=? WHERE id=?",
                (json.dumps(patch, ensure_ascii=False), workspace.diff,
                 json.dumps({**(workspace.verification or {}),
                             "_rounds": workspace.verification_history}
                            if workspace.verification or workspace.verification_history else None,
                            ensure_ascii=False),
                 json.dumps(review, ensure_ascii=False) if review is not None else None,
                 time.time(), repair_id),
            )

    def publish(self, row, check):
        check()
        if not row["review"] or not row["verification"]:
            raise Conflict("repair_review_required")
        review = json.loads(row["review"])
        verification = json.loads(row["verification"])
        if not review.get("approved") or not verification or not verification.get("passed"):
            raise Conflict("repair_review_required")
        source = self.execution.directory(row["env_id"]) / "source"
        current_digest = hashlib.sha256(
            json.dumps(source_tree(source, check), sort_keys=True).encode()
        ).hexdigest()
        if current_digest != row["source_digest"]:
            raise Conflict("repair_source_changed")
        self.event(row["id"], "publishing")
        with self.store.connection() as db:
            db.execute("UPDATE repair_tasks SET stage='publishing' WHERE id=?", (row["id"],))
        context = json.loads(row["context"])
        task = {**dict(row), "account_id": context["account_id"],
                "account_version": context["account_version"]}
        with self.store.connection() as db:
            self.scopes.record_repair(db, row)
        results = "\n".join(
            f"- {key}: exit {value['exit_code']}"
            for key, value in verification.items()
            if isinstance(value, dict) and "exit_code" in value
        )
        body = (
            "## 问题与复现\n\n" + context["finding"]["explanation"] + "\n\n"
            + "\n".join(
                f"{i}. {step}" for i, step in enumerate(context["finding"]["reproduction"], 1)
            )
            + "\n\n已由工作台用户确认复现。"
            + (context["confirmation"].get("note") or "")
            + "\n\n## 修改摘要\n\n" + json.loads(row["patch"])["summary"]
            + "\n\n## 验证结果\n\n" + results + "\n\n## 独立审查\n\n" + review["summary"]
            + "\n\n风险与限制：\n" + "\n".join("- " + risk for risk in review["risks"])
            + "\n\n基于版本 `" + row["revision"] + "`。请仓库拥有者审核并决定是否合并。"
        )
        remote = self.publisher.publish(
            task, json.loads(row["patch"]), redact_values(body), check=check,
        )
        with self.store.connection() as current:
            current.execute(
                "UPDATE repair_tasks SET status='pr_created',stage='pr_created',remote=?,"
                "lease=0,error=NULL,version=version+1,updated=? WHERE id=? AND owner=?",
                (json.dumps(remote, ensure_ascii=False), time.time(), row["id"], self.owner),
            )
        self.event(row["id"], "pr_created", remote["pr"]["url"])
        return self.get(row["id"])

    async def run(self):
        while not self.closing.is_set():
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).warning("Repair worker will retry.")
            await asyncio.sleep(1)
