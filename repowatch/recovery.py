"""Shared ownership, durable usage and receipts for existing business workers."""

import hashlib
import json
import time

from .store import Conflict

POLICY = "workflow-recovery-v1"
MAX_REQUESTS = None
MAX_TOKENS = None
MAX_SECONDS = None
MAX_ACTIONS = None
MAX_CORRECTIONS = None
MAX_RESUMPTIONS = None
LEASE_SECONDS = 600
DELAYS = (10, 30, 60)
FLOWS = {"analysis", "repair", "acceptance", "coordination", "maintenance"}
AUTOMATIC_ERRORS = {
    "repair_model_timeout",
    "repair_provider_unreachable",
    "repair_interrupted",
    "repair_output_truncated",
    "repair_tool_arguments_invalid",
    "repair_handoff_incomplete",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS recovery_configuration (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1, enabled_since REAL
        );
        INSERT OR IGNORE INTO recovery_configuration(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS recovery_roots (
            id TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0,
            corrections INTEGER NOT NULL DEFAULT 0, resumptions INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
            cost REAL, owner TEXT, action_id TEXT, lease REAL NOT NULL DEFAULT 0,
            stop_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS recovery_actions (
            id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES recovery_roots(id),
            flow TEXT NOT NULL, task_id TEXT NOT NULL, basis TEXT NOT NULL,
            state TEXT NOT NULL, owner TEXT NOT NULL, started REAL NOT NULL,
            finished REAL, receipt TEXT
        );
        CREATE TABLE IF NOT EXISTS recovery_usage (
            action_id TEXT NOT NULL REFERENCES recovery_actions(id), event_key TEXT NOT NULL,
            kind TEXT NOT NULL, amount REAL NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(action_id,event_key)
        );
        CREATE INDEX IF NOT EXISTS recovery_task ON recovery_actions(flow,task_id);
        CREATE TABLE IF NOT EXISTS recovery_dispatches (
            id TEXT PRIMARY KEY, root_id TEXT NOT NULL, task_id TEXT NOT NULL,
            source_version INTEGER NOT NULL, configuration_version INTEGER NOT NULL,
            created REAL NOT NULL, receipt TEXT NOT NULL
        );
    """)
    if "stop_reason" not in _columns(db, "recovery_roots"):
        db.execute("ALTER TABLE recovery_roots ADD COLUMN stop_reason TEXT")


def root_id(db, flow, task):
    if flow == "analysis":
        return task["id"]
    return task["analysis_id"]


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _seed(db, root):
    """Import known pre-upgrade counters once, without executing historical failures."""
    requests = corrections = resumptions = 0
    if "requests" in _columns(db, "analysis_report_recovery"):
        saved = db.execute(
            "SELECT * FROM analysis_report_recovery WHERE analysis_id=?", (root,)
        ).fetchone()
        snapshots = [dict(saved)] if saved else []
        if "snapshot" in _columns(db, "analysis_report_recovery_history"):
            snapshots.extend(
                json.loads(row[0])
                for row in db.execute(
                    "SELECT snapshot FROM analysis_report_recovery_history WHERE analysis_id=?",
                    (root,),
                )
            )
        for snapshot in snapshots:
            requests += snapshot["requests"]
            resumptions += snapshot["resumptions"]
            corrections += sum(
                value
                for key, value in json.loads(snapshot["retry_budget"]).items()
                if key.endswith("used") and type(value) is int
            )
    if "analysis_id" in _columns(db, "repair_tasks"):
        requests += db.execute(
            "SELECT COUNT(*) FROM repair_events e JOIN repair_tasks t ON t.id=e.repair_id "
            "WHERE t.analysis_id=? AND e.kind='request_started'",
            (root,),
        ).fetchone()[0]
        resumptions += db.execute(
            "SELECT COALESCE(SUM(MAX(attempt-1,0)),0) FROM repair_tasks WHERE analysis_id=?",
            (root,),
        ).fetchone()[0]
    if "model_calls" in _columns(db, "scope_acceptance_runs"):
        requests += db.execute(
            "SELECT COALESCE(SUM(model_calls),0) FROM scope_acceptance_runs WHERE analysis_id=?",
            (root,),
        ).fetchone()[0]
    db.execute(
        "UPDATE recovery_roots SET requests=?,corrections=?,resumptions=? WHERE id=?",
        (requests, corrections, resumptions, root),
    )


def claim(db, flow, task, attempt, basis, owner, lease, now=None):
    """Caller holds BEGIN IMMEDIATE; business claim and ownership commit together."""
    if flow not in FLOWS:
        raise Conflict("recovery_policy_invalid")
    now = time.time() if now is None else now
    root = root_id(db, flow, task)
    inserted = db.execute("INSERT OR IGNORE INTO recovery_roots(id) VALUES(?)", (root,)).rowcount
    if inserted:
        _seed(db, root)
    saved = db.execute("SELECT * FROM recovery_roots WHERE id=?", (root,)).fetchone()
    if saved["action_id"] and saved["lease"] > now:
        return None
    action = digest([POLICY, root, flow, task["id"], attempt, basis])
    if db.execute("SELECT 1 FROM recovery_actions WHERE id=?", (action,)).fetchone():
        return None
    expire(db, root, now)
    db.execute(
        "INSERT INTO recovery_actions VALUES(?,?,?,?,?, 'running',?,?,NULL,NULL)",
        (action, root, flow, task["id"], digest(basis), owner, now),
    )
    db.execute(
        "UPDATE recovery_roots SET owner=?,action_id=?,lease=?,"
        "stop_reason=CASE WHEN stop_reason='recovery_budget_exhausted' THEN NULL "
        "ELSE stop_reason END WHERE id=?",
        (owner, action, lease, root),
    )
    if db.execute(
        "SELECT 1 FROM recovery_actions WHERE flow=? AND task_id=? AND id<>?",
        (flow, task["id"], action),
    ).fetchone():
        db.execute(
            "INSERT INTO recovery_usage VALUES(?,'resume','resumptions',1,?)", (action, now)
        )
        db.execute("UPDATE recovery_roots SET resumptions=resumptions+1 WHERE id=?", (root,))
    return action


def expire(db, root, now=None):
    """Settle an abandoned action in the same transaction as business interruption."""
    now = time.time() if now is None else now
    saved = db.execute(
        "SELECT * FROM recovery_roots WHERE id=? AND action_id IS NOT NULL AND lease<=?",
        (root, now),
    ).fetchone()
    if not saved:
        return
    old = db.execute(
        "SELECT started FROM recovery_actions WHERE id=?", (saved["action_id"],)
    ).fetchone()
    db.execute(
        "UPDATE recovery_actions SET state='interrupted',finished=?,receipt=? "
        "WHERE id=? AND state='running'",
        (
            now,
            json.dumps(
                {
                    "reason": "lease_expired",
                    "execution": "unknown",
                }
            ),
            saved["action_id"],
        ),
    )
    db.execute(
        "UPDATE recovery_roots SET seconds=seconds+?,owner=NULL,action_id=NULL,lease=0 WHERE id=?",
        (max(0, saved["lease"] - old["started"]), root),
    )


def active(db, action, owner, now=None):
    now = time.time() if now is None else now
    return db.execute(
        "SELECT a.* FROM recovery_actions a JOIN recovery_roots r "
        "ON r.id=a.root_id WHERE a.id=? AND a.owner=? AND a.state='running' "
        "AND r.action_id=a.id AND r.owner=? AND r.lease>?",
        (action, owner, owner, now),
    ).fetchone()


def require(db, action, owner):
    saved = active(db, action, owner)
    if not saved:
        raise Conflict("recovery_ownership_changed")
    from .release_state import require_source_authorization

    require_source_authorization(db, saved["flow"], saved["task_id"])
    # Active workers renew a finite lease. Duration is never a task quota;
    # a vanished worker still becomes recoverable after the last heartbeat.
    now = time.time()
    root = db.execute("SELECT lease FROM recovery_roots WHERE id=?",
                      (saved["root_id"],)).fetchone()
    if root["lease"] < now + LEASE_SECONDS / 2:
        lease = now + LEASE_SECONDS
        changed = db.execute(
            "UPDATE recovery_roots SET lease=? WHERE id=? AND action_id=? AND owner=? AND lease>?",
            (lease, saved["root_id"], action, owner, now),
        ).rowcount
        if not changed:
            raise Conflict("recovery_ownership_changed")
        table = {"analysis": "repository_analyses", "repair": "repair_tasks",
                 "acceptance": "scope_acceptance_runs", "coordination": "coordination_jobs",
                 "maintenance": "maintenance_tasks"}[saved["flow"]]
        column = ("state" if saved["flow"] in {"acceptance", "coordination", "maintenance"}
                  else "status")
        state = "diagnosing" if saved["flow"] == "coordination" else "running"
        if not db.execute(
            f"UPDATE {table} SET lease=? WHERE id=? AND {column}=?",
            (lease, saved["task_id"], state),
        ).rowcount:
            raise Conflict("recovery_ownership_changed")


def spend(db, action, owner, event_key, kind, amount=1):
    """Reserve before execution; stable keys make duplicate delivery free."""
    if kind == "tokens":
        # Late usage is an accounting fact, not permission to resume an old action.
        saved = db.execute(
            "SELECT * FROM recovery_actions WHERE id=? AND owner=?", (action, owner)
        ).fetchone()
    else:
        require(db, action, owner)
        saved = active(db, action, owner)
    if not saved:
        raise Conflict("recovery_ownership_changed")
    if kind not in {"requests", "corrections", "resumptions", "tokens", "seconds"}:
        raise Conflict("recovery_usage_invalid")
    if type(amount) not in (int, float) or not 0 <= amount <= 10000000:
        raise Conflict("recovery_usage_invalid")
    if db.execute(
        "SELECT 1 FROM recovery_usage WHERE action_id=? AND event_key=?", (action, event_key)
    ).fetchone():
        return
    db.execute(
        "INSERT INTO recovery_usage VALUES(?,?,?,?,?)",
        (action, event_key, kind, amount, time.time()),
    )
    db.execute(f"UPDATE recovery_roots SET {kind}={kind}+? WHERE id=?", (amount, saved["root_id"]))


def finish(db, action, owner, state, reason=None, now=None):
    now = time.time() if now is None else now
    saved = active(db, action, owner, now=0)
    if not saved:
        return
    # Business state is authoritative; an action receipt never resolves a fault.
    db.execute(
        "UPDATE recovery_actions SET state=?,finished=?,receipt=? WHERE id=?",
        (
            "completed" if state in {"completed", "pr_created"} else "blocked",
            now,
            json.dumps({"source_state": state, "reason": reason}),
            action,
        ),
    )
    db.execute(
        "UPDATE recovery_roots SET owner=NULL,action_id=NULL,lease=0 WHERE id=? AND action_id=?",
        (saved["root_id"], action),
    )


def observe(db, action, owner, kind, value=""):
    if kind in {"analysis_request", "request_started"}:
        metric, amount = "requests", 1
    elif kind.endswith("_retry") or kind in {"review_feedback", "verification_feedback"}:
        metric, amount = "corrections", 1
    elif kind == "usage":
        number = db.execute(
            "SELECT COUNT(*) FROM recovery_usage WHERE action_id=? AND kind='requests'", (action,)
        ).fetchone()[0]
        spend(db, action, owner, f"tokens:request:{number}", "tokens", value)
        return
    else:
        return
    number = db.execute(
        "SELECT COUNT(*) FROM recovery_usage WHERE action_id=? AND kind=?", (action, metric)
    ).fetchone()[0]
    spend(db, action, owner, f"{metric}:{number}", metric, amount)


def release(store, action, owner, table, task_id, status="status"):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"SELECT {status},error FROM {table} WHERE id=?", (task_id,)).fetchone()
        saved = active(db, action, owner, now=0)
        if saved:
            db.execute(
                "UPDATE recovery_roots SET seconds=seconds+? WHERE id=?",
                (max(0, time.time() - saved["started"]), saved["root_id"]),
            )
        finish(db, action, owner, row[0] if row else "missing", row[1] if row else None)


def state(db):
    saved = dict(db.execute("SELECT * FROM recovery_configuration WHERE id=1").fetchone())
    return {
        **saved,
        "enabled": bool(saved["enabled"]),
        "policy": POLICY,
        "max_requests": MAX_REQUESTS,
        "max_tokens": MAX_TOKENS,
        "max_seconds": MAX_SECONDS,
        "max_corrections": MAX_CORRECTIONS,
        "max_resumptions": MAX_RESUMPTIONS,
        "max_automatic_actions": MAX_ACTIONS,
        "delays": list(DELAYS),
    }


def snapshot(db, root):
    saved = db.execute("SELECT * FROM recovery_roots WHERE id=?", (root,)).fetchone()
    if not saved:
        return None
    return {
        "stop_reason": saved["stop_reason"],
        "budget": {
            key: saved[key]
            for key in (
                "requests",
                "corrections",
                "resumptions",
                "tokens",
                "seconds",
                "cost",
            )
        },
        "actions": [
            dict(row)
            for row in db.execute(
                "SELECT id,flow,task_id,basis,state,started,finished,receipt FROM recovery_actions "
                "WHERE root_id=? ORDER BY started DESC LIMIT 100",
                (root,),
            )
        ],
    }


def automatic_guard(db, context):
    version = context.get("_coordination_version")
    if version is not None:
        settings = db.execute(
            "SELECT enabled,version FROM coordination_configuration WHERE id=1",
        ).fetchone()
        if not settings or not settings[0] or settings[1] != version:
            raise Conflict("recovery_disabled")
    version = context.get("_automatic_recovery_version")
    if version is not None:
        settings = state(db)
        if not settings["enabled"] or settings["version"] != version:
            raise Conflict("recovery_disabled")


def authorize_automatic(db, task, binding, now=None):
    """Called in the existing retry transaction; never grants new task permissions."""
    now = time.time() if now is None else now
    settings = state(db)
    context = json.loads(task["context"])
    last = db.execute(
        "SELECT * FROM recovery_actions WHERE flow='repair' AND task_id=? "
        "ORDER BY started DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    count = db.execute(
        "SELECT COUNT(*) FROM recovery_dispatches WHERE root_id=?", (task["analysis_id"],)
    ).fetchone()[0]
    root = db.execute("SELECT * FROM recovery_roots WHERE id=?", (task["analysis_id"],)).fetchone()
    if (
        not settings["enabled"]
        or task["error"] not in AUTOMATIC_ERRORS
        or not last
        or last["finished"] is None
        or last["finished"] < settings["enabled_since"]
        or context.get("_recovery_binding") != binding
        or now < task["updated"] + DELAYS[min(count, len(DELAYS) - 1)]
        or not root
        or root["action_id"]
    ):
        raise Conflict("recovery_automatic_unavailable")
    action = digest([POLICY, "retry", task["id"], task["version"]])
    if db.execute("SELECT 1 FROM recovery_dispatches WHERE id=?", (action,)).fetchone():
        raise Conflict("recovery_action_exists")
    db.execute(
        "INSERT INTO recovery_dispatches VALUES(?,?,?,?,?,?,?)",
        (
            action,
            task["analysis_id"],
            task["id"],
            task["version"],
            settings["version"],
            now,
            json.dumps({"state": "queued", "reason": task["error"],
                        "delay": DELAYS[min(count, len(DELAYS) - 1)]}),
        ),
    )
    return settings["version"]


def configure(db, enabled, version):
    changed = db.execute(
        "UPDATE recovery_configuration SET enabled=?,version=version+1,"
        "enabled_since=CASE WHEN enabled=0 AND ?=1 THEN ? "
        "ELSE enabled_since END WHERE id=1 AND version=?",
        (enabled, enabled, time.time(), version),
    ).rowcount
    if not changed:
        raise Conflict("recovery_configuration_changed")
    return state(db)
