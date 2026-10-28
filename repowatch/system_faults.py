"""Local fault ledger. Consumes facts only; never invokes business workers or models."""

import asyncio
import json
import logging
import sqlite3
import threading
import time

from .fault_contract import STATES, FaultEvent, fingerprint, note, token
from .store import Conflict, Missing, uid

LOG = logging.getLogger(__name__)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS system_faults (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_fault_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_event_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS fault_outbox_pending ON system_fault_outbox(consumed,id);
        CREATE TABLE IF NOT EXISTS system_fault_occurrences (
            id TEXT PRIMARY KEY, fault_id TEXT NOT NULL REFERENCES system_faults(id),
            source_event_key TEXT NOT NULL UNIQUE, repo_id TEXT, account_id INTEGER, env_id TEXT,
            task_id TEXT NOT NULL, attempt INTEGER, flow TEXT NOT NULL,
            stage TEXT, code TEXT NOT NULL,
            occurred REAL NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS fault_scope ON system_fault_occurrences(account_id,repo_id);
        CREATE INDEX IF NOT EXISTS fault_group ON system_fault_occurrences(fault_id,occurred);
        CREATE INDEX IF NOT EXISTS fault_task ON system_fault_occurrences(task_id,attempt);
        CREATE TABLE IF NOT EXISTS system_fault_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurrence_id TEXT NOT NULL REFERENCES system_fault_occurrences(id),
            state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
            evidence TEXT, created REAL NOT NULL, source_event_key TEXT, details TEXT,
            UNIQUE(occurrence_id,source_event_key)
        );
        CREATE TABLE IF NOT EXISTS system_fault_ingestion (
            source TEXT PRIMARY KEY, cursor TEXT NOT NULL, last_success REAL,
            failures INTEGER NOT NULL DEFAULT 0, error TEXT
        );
        CREATE TABLE IF NOT EXISTS system_fault_configuration (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
            installed REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1
        );
    """)
    db.execute(
        "INSERT OR IGNORE INTO system_fault_configuration(id,installed) VALUES(1,?)", (time.time(),)
    )
    from .fault_sources import initialize_cursors

    initialize_cursors(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(system_fault_configuration)")}
    if "version" not in columns:
        db.execute("ALTER TABLE system_fault_configuration ADD COLUMN version INTEGER DEFAULT 1")
    columns = {row[1] for row in db.execute("PRAGMA table_info(system_fault_history)")}
    if "details" not in columns:
        db.execute("ALTER TABLE system_fault_history ADD COLUMN details TEXT")
    columns = {row[1] for row in db.execute("PRAGMA table_info(execution_diagnoses)")}
    for column in (
        "fault_diagnostic",
        "fault_model_binding",
        "fault_role",
        "fault_workbench_version",
    ):
        if column not in columns:
            db.execute(f"ALTER TABLE execution_diagnoses ADD COLUMN {column} TEXT")
    columns = {row[1] for row in db.execute("PRAGMA table_info(execution_jobs)")}
    if "fault_basis" not in columns:
        db.execute("ALTER TABLE execution_jobs ADD COLUMN fault_basis TEXT")


def enqueue(db, event: FaultEvent):
    safe = event.safe()
    db.execute(
        "INSERT OR IGNORE INTO system_fault_outbox(source_event_key,kind,payload,created) "
        "VALUES(?,'fault',?,?)",
        (safe["source_event_key"], json.dumps(safe), time.time()),
    )


def transition(
    db, occurrence, state, *, actor, reason, evidence, source_event_key=None, details=None
):
    if state not in STATES:
        raise ValueError("fault_state_invalid")
    # Only the source adapter may provide a durable verification reference.
    if state == "resolved" and (actor != "source" or not evidence):
        raise Conflict("fault_verification_required")
    if occurrence["state"] == "dismissed" or occurrence["state"] == state:
        return
    if (
        source_event_key
        and db.execute(
            "SELECT 1 FROM system_fault_history WHERE occurrence_id=? AND source_event_key=?",
            (occurrence["id"], source_event_key),
        ).fetchone()
    ):
        return
    db.execute(
        "UPDATE system_fault_occurrences SET state=?,version=version+1 WHERE id=?",
        (state, occurrence["id"]),
    )
    db.execute(
        "INSERT INTO system_fault_history(occurrence_id,state,actor,reason,evidence,created,"
        "source_event_key,details) VALUES(?,?,?,?,?,?,?,?)",
        (
            occurrence["id"],
            state,
            actor,
            note(reason),
            token(evidence),
            time.time(),
            source_event_key,
            json.dumps(details or {}),
        ),
    )


def consume(db, limit=200):
    rows = db.execute(
        "SELECT * FROM system_fault_outbox WHERE consumed=0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    for row in rows:
        event = json.loads(row["payload"])
        if row["kind"] == "fault":
            group_key = fingerprint(event)
            db.execute(
                "INSERT OR IGNORE INTO system_faults VALUES(?,?,?)",
                (uid("fault"), group_key, event["occurred"]),
            )
            fault_id = db.execute(
                "SELECT id FROM system_faults WHERE fingerprint=?", (group_key,)
            ).fetchone()[0]
            occurrence_id = uid("occurrence")
            inserted = db.execute(
                "INSERT OR IGNORE INTO system_fault_occurrences(id,fault_id,source_event_key,"
                "repo_id,account_id,env_id,task_id,attempt,flow,stage,code,occurred,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    occurrence_id,
                    fault_id,
                    event["source_event_key"],
                    event["repo_id"],
                    event["account_id"],
                    event["env_id"],
                    event["task_id"],
                    event["attempt"],
                    event["flow"],
                    event["stage"],
                    event["code"],
                    event["occurred"],
                    row["payload"],
                ),
            ).rowcount
            if inserted:
                db.execute(
                    "INSERT INTO system_fault_history(occurrence_id,state,actor,reason,"
                    "evidence,created,source_event_key) VALUES(?,'pending','source',?,?,?,?)",
                    (
                        occurrence_id,
                        "历史补录" if event["historical"] else "来源故障已记录",
                        event["source_event_key"],
                        event["occurred"],
                        event["source_event_key"],
                    ),
                )
        else:
            # The adapter scopes observations to the same source, binding and attempt.
            candidates = db.execute(
                "SELECT * FROM system_fault_occurrences WHERE task_id=? AND occurred<=? "
                "AND state NOT IN ('dismissed','resolved')",
                (event["task_id"], event["occurred"]),
            ).fetchall()
            for occurrence in candidates:
                payload = json.loads(occurrence["payload"])
                if (
                    not event.get("verified_models")
                    and event.get("attempt") != occurrence["attempt"]
                    or event.get("flow") != occurrence["flow"]
                ):
                    continue
                if event.get("code") and event["code"] != occurrence["code"]:
                    continue
                if event.get("verified_models"):
                    if (
                        any(
                            payload[key] != event.get(key)
                            for key in (
                                "repo_id",
                                "account_id",
                                "env_id",
                                "revision",
                                "workbench_version",
                            )
                        )
                        or not payload["model_binding"]
                        or payload["model_binding"] not in event["verified_models"]
                    ):
                        continue
                elif event.get("role") and payload["role"] != event["role"]:
                    continue
                transition(
                    db,
                    occurrence,
                    event["state"],
                    actor="source",
                    reason=event["reason"],
                    evidence=event["source_event_key"],
                    source_event_key=event["source_event_key"],
                    details={key: event.get(key, {}) for key in ("diagnostic", "budget")},
                )
        db.execute("UPDATE system_fault_outbox SET consumed=1 WHERE id=?", (row["id"],))
    return len(rows)


def aggregate(counts):
    counts = {state: counts.get(state, 0) for state in STATES}
    remaining = sum(counts[state] for state in STATES if state not in ("resolved", "dismissed"))
    state = next(
        (
            state
            for state in ("blocked", "recovering", "verifying", "diagnosing", "pending")
            if counts[state]
        ),
        "dismissed" if counts["dismissed"] else "resolved",
    )
    return {"state": state, "remaining": remaining, "state_counts": counts}


class SystemFaults:
    def __init__(self, store):
        self.store, self.closing = store, threading.Event()

    def tick(self):
        try:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT enabled FROM system_fault_configuration").fetchone()[0]:
                    return 0
                from .fault_sources import capture

                capture(db, self.store.workbench_version)
                count = consume(db)
                db.execute(
                    "INSERT INTO system_fault_ingestion VALUES('collector','0',?,0,NULL) "
                    "ON CONFLICT(source) DO UPDATE SET "
                    "last_success=excluded.last_success,error=NULL",
                    (time.time(),),
                )
                db.execute(
                    "INSERT INTO system_fault_ingestion VALUES('source_capture','0',?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET failures=excluded.failures,"
                    "error=excluded.error,last_success=excluded.last_success",
                    (
                        time.time(),
                        self.store.fault_capture_failures,
                        self.store.fault_ingestion_error,
                    ),
                )
            self.store.fault_ingestion_error = None
            return count
        except Exception as exc:
            self.store.fault_ingestion_error = type(exc).__name__[:60]
            return 0

    async def run(self):
        while not self.closing.is_set():
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(2)

    def health(self):
        try:
            with self.store.connection() as db:
                config = db.execute("SELECT * FROM system_fault_configuration").fetchone()
                rows = [dict(row) for row in db.execute("SELECT * FROM system_fault_ingestion")]
                pending = db.execute(
                    "SELECT count(*) FROM system_fault_outbox WHERE consumed=0"
                ).fetchone()[0]
            return {
                "enabled": bool(config["enabled"]),
                "version": config["version"],
                "installed": config["installed"],
                "error": self.store.fault_ingestion_error,
                "pending": pending,
                "sources": rows,
                "mode": "record_only",
            }
        except sqlite3.Error:
            return {
                "enabled": None,
                "error": "storage_unavailable",
                "pending": None,
                "sources": [],
                "mode": "record_only",
            }

    @staticmethod
    def _scope(db, alias="o"):
        # Single local owner; repository facts still require the original active identity.
        return (
            f"({alias}.repo_id IS NULL OR EXISTS(SELECT 1 FROM repositories r "
            f"JOIN environments e ON e.repo_id=r.id "
            "JOIN github_configuration c ON c.active_account_id=e.account_id "
            "JOIN github_accounts a ON a.id=c.active_account_id AND a.state='verified' "
            f"WHERE r.id={alias}.repo_id AND e.id={alias}.env_id "
            f"AND e.account_id={alias}.account_id))"
        )

    def configure(self, version, enabled):
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE system_fault_configuration SET enabled=?,version=version+1 "
                "WHERE id=1 AND version=?",
                (int(enabled), version),
            ).rowcount
            if not changed:
                raise Conflict("fault_configuration_changed")
        return self.health()

    def _query(self, db, fault_id, *, repo_id=None, env_id=None, task_id=None, flow=None):
        where, args = ["o.fault_id=?", self._scope(db)], [fault_id]
        for key, value in (
            ("repo_id", repo_id),
            ("env_id", env_id),
            ("task_id", task_id),
            ("flow", flow),
        ):
            if value is not None:
                where.append(f"o.{key}=?")
                args.append(value)
        return " FROM system_fault_occurrences o WHERE " + " AND ".join(where), args

    def _counts(self, db, fault_id, **filters):
        query, args = self._query(db, fault_id, **filters)
        return {
            row[0]: row[1]
            for row in db.execute(
                "SELECT o.state,count(*)" + query + " GROUP BY o.state",
                args,
            )
        }

    def list(self, *, limit=50, offset=0, state=None, **filters):
        if state is not None and state not in STATES:
            raise Conflict("fault_state_invalid")
        if not 1 <= limit <= 200 or offset < 0:
            raise Conflict("fault_pagination_invalid")
        # Group rows first in SQL; no loading of the complete ledger into Python.
        with self.store.connection() as db:
            where, args = [self._scope(db)], []
            for key in ("repo_id", "env_id", "task_id", "flow"):
                if filters.get(key) is not None:
                    where.append(f"o.{key}=?")
                    args.append(filters[key])
            precedence = (
                "blocked",
                "recovering",
                "verifying",
                "diagnosing",
                "pending",
                "dismissed",
                "resolved",
            )
            rank = (
                "CASE o.state "
                + " ".join(f"WHEN '{item}' THEN {index}" for index, item in enumerate(precedence))
                + " END"
            )
            query = (
                "SELECT o.fault_id,min(o.occurred) first_seen,max(o.occurred) last_seen,"
                f"count(*) occurrences,min({rank}) rank FROM system_fault_occurrences o "
                "WHERE " + " AND ".join(where) + " GROUP BY o.fault_id"
            )
            if state:
                query += " HAVING rank=?"
                args.append(precedence.index(state))
            total = db.execute("SELECT count(*) FROM (" + query + ")", args).fetchone()[0]
            groups = db.execute(
                query + " ORDER BY last_seen DESC,fault_id LIMIT ? OFFSET ?", [*args, limit, offset]
            ).fetchall()
            items = []
            for group in groups:
                source_query, source_args = self._query(db, group["fault_id"], **filters)
                latest = json.loads(
                    db.execute(
                        "SELECT o.payload"
                        + source_query
                        + " ORDER BY o.occurred DESC,o.id LIMIT 1",
                        source_args,
                    ).fetchone()[0]
                )
                items.append(
                    {
                        "id": group["fault_id"],
                        **dict(group),
                        **aggregate(self._counts(db, group["fault_id"], **filters)),
                        **{
                            key: latest[key]
                            for key in (
                                "flow",
                                "stage",
                                "role",
                                "code",
                                "category",
                                "repo_id",
                                "env_id",
                                "historical",
                            )
                        },
                    }
                )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def detail(self, fault_id, *, limit=50, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise Conflict("fault_pagination_invalid")
        with self.store.connection() as db:
            counts = self._counts(db, fault_id)
            if not counts:
                raise Missing("Fault not found.")
            query, args = self._query(db, fault_id)
            rows = db.execute(
                "SELECT o.*" + query + " ORDER BY o.occurred DESC,o.id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                items.append(
                    {
                        **json.loads(row["payload"]),
                        "id": row["id"],
                        "state": row["state"],
                        "version": row["version"],
                    }
                )
            return {
                "id": fault_id,
                **aggregate(counts),
                "occurrences": sum(counts.values()),
                "items": items,
                "limit": limit,
                "offset": offset,
            }

    def history(self, fault_id, *, limit=50, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise Conflict("fault_pagination_invalid")
        with self.store.connection() as db:
            if not self._counts(db, fault_id):
                raise Missing("Fault not found.")
            where = (
                "FROM system_fault_history h JOIN system_fault_occurrences o "
                "ON o.id=h.occurrence_id WHERE o.fault_id=? AND " + self._scope(db)
            )
            total = db.execute("SELECT count(*) " + where, (fault_id,)).fetchone()[0]
            items = [
                {**dict(row), "details": json.loads(row["details"] or "{}")}
                for row in db.execute(
                    "SELECT h.* " + where + " ORDER BY h.id DESC LIMIT ? OFFSET ?",
                    (fault_id, limit, offset),
                )
            ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def dismiss(self, fault_id, occurrence_id, version, reason):
        reason = note(reason)
        if not reason:
            raise Conflict("fault_close_reason_required")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            query, args = self._query(db, fault_id)
            row = db.execute(
                "SELECT o.*" + query + " AND o.id=?", [*args, occurrence_id]
            ).fetchone()
            if not row:
                raise Missing("Fault occurrence not found.")
            if row["version"] != version or row["state"] in ("resolved", "dismissed"):
                raise Conflict("fault_version_changed")
            transition(db, row, "dismissed", actor="owner", reason=reason, evidence=None)
        return self.detail(fault_id)

    def import_history(self, *, limit=200):
        # Explicit, bounded, resumable metadata import; does not call any workflow.
        from .fault_sources import capture

        if not 1 <= limit <= 200:
            raise Conflict("fault_pagination_invalid")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = capture(db, None, historical=True, limit=limit)
            consume(db, limit)
        return {"scanned": count, "health": self.health()}


def capture_safely(store, db, *, live_after=None):
    """Called before source commit. A ledger failure cannot roll back the source fact."""
    savepoint = False
    try:
        db.execute("SAVEPOINT fault_capture")
        savepoint = True
        from .fault_sources import capture

        capture(db, store.workbench_version, live_after=live_after)
        db.execute("RELEASE fault_capture")
    except Exception as exc:
        if savepoint:
            db.execute("ROLLBACK TO fault_capture")
            db.execute("RELEASE fault_capture")
        if store.fault_ingestion_error is None:
            LOG.warning("System fault capture unavailable; persisted sources can be replayed.")
        store.fault_ingestion_error = type(exc).__name__[:60]
        store.fault_capture_failures = min(store.fault_capture_failures + 1, 2**31 - 1)


def record_worker_failure(store, worker, code="worker_failed"):
    """Observe a local worker failure without retaining its exception message."""
    event_id = uid("service_event")
    try:
        with store.connection() as db:
            enqueue(
                db,
                FaultEvent(
                    source_event_key=f"service:{event_id}",
                    flow="service",
                    task_id=worker,
                    source_table="service",
                    source_id=event_id,
                    occurred=time.time(),
                    stage=worker,
                    code=code,
                    workbench_version=store.workbench_version,
                ),
            )
    except Exception as exc:
        store.fault_ingestion_error = type(exc).__name__[:60]
