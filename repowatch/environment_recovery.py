"""Pre-model Docker waits. No model call, publication or attempt reset lives here."""

import hashlib
import json
import time

from .repair_workspace import source_tree
from .store import Conflict

WAITING = "waiting_environment"


def legacy_preflight_recovery(db, row):
    """Only an explicit retry may recover an audited old pre-model failure once."""
    context = json.loads(row["context"])
    if (row["attempt"] < 1 or row["status"] != "failed" or row["stage"] != "repair_agent"
            or row["error"] != "docker_unavailable"
            or any(row[key] for key in ("patch", "verification", "review", "diff", "remote"))
            or context.get("environment_recovery_used")):
        return False
    details, boundary = None, False
    for event in db.execute(
        "SELECT kind,value FROM repair_events WHERE repair_id=? ORDER BY id DESC LIMIT 256",
        (row["id"],),
    ):
        if event["kind"] in ("retry", "queued"):
            boundary = (event["value"] == str(row["attempt"]) if event["kind"] == "retry"
                        else row["attempt"] <= 1)
            break
        if event["kind"] in (
            "workspace_recovery", "source_read_recovery", "failure_recovery",
            "timeout_recovery", "output_recovery", "protocol_recovery",
        ):
            continue
        if event["kind"] == "failure_details":
            try:
                details = json.loads(event["value"])
            except (ValueError, TypeError):
                return False
        elif event["kind"] != "failure":
            return False
    if not boundary or not isinstance(details, dict):
        return False
    locations = details.get("locations")
    return (
        details.get("code") == "docker_unavailable"
        and details.get("exceptions") == ["ExecutionError"]
        and isinstance(locations, list) and bool(locations)
        and all(isinstance(loc, dict) and loc.get("file") == "repowatch/repairs.py"
                for loc in locations)
    )


class EnvironmentRecovery:
    @staticmethod
    def _docker_recovery(db, row):
        return legacy_preflight_recovery(db, row)

    def _environment_candidate(self, *, allow_work):
        if not allow_work or self.closing.is_set() or self.execution.closing.is_set():
            return None
        with self.store.connection() as db:
            if not db.execute("SELECT enabled FROM repair_settings WHERE id=1").fetchone()[0]:
                return None
            if db.execute("SELECT 1 FROM repair_tasks WHERE status='running'").fetchone():
                return None
            row = db.execute(
                "SELECT * FROM repair_tasks WHERE status IN ('queued',?) "
                "ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END,created LIMIT 1", (WAITING,),
            ).fetchone()
            if not row:
                return None
            row = dict(row)
        if self._verified(row):
            return row
        context = json.loads(row["context"])
        waiting = context.get("environment_wait", {})
        now = time.time()
        if row["status"] == WAITING and waiting.get("next_check", 0) > now:
            return None
        # No SQLite write lock is held across Docker or filesystem I/O.
        ready = self.execution.runtime.readiness()
        digest, failure = None, None
        try:
            tree = source_tree(self.execution.directory(row["env_id"]) / "source")
            digest = hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()
        except (OSError, Conflict):
            failure = "repair_source_changed"
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT * FROM repair_tasks WHERE id=?", (row["id"],)).fetchone()
            if (not latest or latest["version"] != row["version"]
                    or latest["status"] != row["status"]):
                return None
            if (not db.execute("SELECT enabled FROM repair_settings WHERE id=1").fetchone()[0]
                    or not self.engine.status()["enabled"] or self.closing.is_set()
                    or self.execution.closing.is_set()):
                return None
            if not self.current(db, latest):
                failure = "repair_stale"
            account = db.execute(
                "SELECT a.id,a.version FROM github_accounts a JOIN github_configuration c "
                "ON c.active_account_id=a.id WHERE a.state='verified'",
            ).fetchone()
            if not account:
                failure = "github_active_account_required"
            elif waiting.get("account_id", account["id"]) != account["id"]:
                failure = "repair_account_changed"
            if waiting.get("source_digest", digest) != digest:
                failure = "repair_source_changed"
            if not ready["ready"] and ready["reason"] != "docker_unavailable":
                failure = ready["reason"]
            if failure:
                db.execute(
                    "UPDATE repair_tasks SET status='failed',error=?,version=version+1,updated=? "
                    "WHERE id=?", (failure, now, row["id"]),
                )
                db.execute("INSERT INTO repair_events(repair_id,kind,value,created) "
                           "VALUES(?,'failure',?,?)", (row["id"], failure, now))
                return None
            if not ready["ready"]:
                checks = min(int(waiting.get("checks", 0)) + 1, 8)
                context["environment_wait"] = {
                    "checks": checks, "next_check": now + min(5 * 2 ** (checks - 1), 60),
                    "account_id": account["id"], "source_digest": digest,
                }
                db.execute(
                    "UPDATE repair_tasks SET status=?,stage='repair_agent',"
                    "error='docker_unavailable',context=?,version=version+1,updated=? WHERE id=?",
                    (WAITING, json.dumps(context), now, row["id"]),
                )
                if row["status"] != WAITING:
                    db.execute("INSERT INTO repair_events(repair_id,kind,value,created) "
                               "VALUES(?,'environment_wait','docker_unavailable',?)",
                               (row["id"], now))
                return None
            if row["status"] == WAITING or "environment_wait" in context:
                context.pop("environment_wait", None)
                context.update(account_id=account["id"], account_version=account["version"],
                               environment_account_id=account["id"])
                db.execute(
                    "UPDATE repair_tasks SET status='queued',error=NULL,context=?,"
                    "version=version+1,updated=? WHERE id=?", (json.dumps(context), now, row["id"]),
                )
                db.execute("INSERT INTO repair_events(repair_id,kind,value,created) "
                           "VALUES(?,'environment_ready','docker_ready',?)", (row["id"], now))
                row = dict(db.execute("SELECT * FROM repair_tasks WHERE id=?",
                                      (row["id"],)).fetchone())
        return {**row, "preflight_source_digest": digest}
