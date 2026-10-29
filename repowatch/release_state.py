"""Release gates and an offline journal; neither depends on models or the Web loop."""

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .store import Conflict, Store


class ExistingStore(Store):
    """Controller access must never run a candidate migration on the original DB."""

    def __init__(self, path):
        self.path = Path(path)
        self.faults_ready = False
        with existing_database(self.path):
            pass


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS release_control (
            id INTEGER PRIMARY KEY CHECK(id=1), draining TEXT
        );
        INSERT OR IGNORE INTO release_control(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS release_authorizations (
            kind TEXT NOT NULL, target TEXT NOT NULL, binding TEXT NOT NULL,
            resume_original INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,target)
        );
        CREATE TABLE IF NOT EXISTS release_events (
            id TEXT PRIMARY KEY, release_id TEXT NOT NULL, kind TEXT NOT NULL,
            reason TEXT, created REAL NOT NULL, details TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS release_resumptions (
            release_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL,
            reason TEXT, created REAL NOT NULL
        );
    """)
    db.execute("INSERT OR IGNORE INTO settings VALUES('release_database_id', ?)",
               (uuid4().hex,))


def draining(db):
    return db.execute("SELECT draining FROM release_control WHERE id=1").fetchone()[0]


def idle(db):
    # Queued/checkpointed work remains untouched. A running action must finish first.
    checks = (
        ("execution_jobs", "state='running'"),
        ("execution_diagnoses", "state='running'"),
        ("repository_analyses", "status='running'"),
        ("repair_tasks", "status='running'"),
        ("scope_acceptance_runs", "state='running'"),
        ("memory_evaluations", "status='running'"),
        ("coordination_jobs", "state='diagnosing'"),
        ("maintenance_tasks", "state='running'"),
    )
    return not any(db.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").fetchone()
                   for table, condition in checks)


def require_source_authorization(db, flow, task_id):
    rows = db.execute(
        "SELECT a.binding,a.revoked,a.resume_original FROM release_resumptions r "
        "JOIN maintenance_tasks m ON m.id=r.task_id "
        "JOIN system_fault_occurrences o ON o.id=m.occurrence_id "
        "LEFT JOIN release_authorizations a ON a.kind='deploy' AND a.target=r.release_id "
        "WHERE r.state='verifying_original' AND o.flow=? AND o.task_id=?",
        (flow, task_id)).fetchall()
    for row in rows:
        if not row["binding"] or row["revoked"] or not row["resume_original"]:
            raise Conflict("release_resume_authorization_revoked")
        binding = json.loads(row["binding"])
        account = db.execute(
            "SELECT a.version,a.state,c.active_account_id FROM github_accounts a "
            "JOIN github_configuration c ON c.id=1 WHERE a.id=?",
            (binding["account_id"],)).fetchone()
        if (not account or account["state"] != "verified"
                or account["version"] != binding["account_version"]
                or account["active_account_id"] != binding["account_id"]):
            raise Conflict("release_account_changed")


def verifies_released_source(db, occurrence, event):
    """Only a source success in its explicitly dispatched release can cross versions."""
    if (event.get("state") != "resolved" or event.get("attempt") is None
            or occurrence["attempt"] is None or event["attempt"] <= occurrence["attempt"]):
        return False
    rows = db.execute(
        "SELECT e.created,e.details FROM release_resumptions r "
        "JOIN maintenance_tasks m ON m.id=r.task_id "
        "JOIN release_events e ON e.release_id=r.release_id "
        "AND e.kind='source_resume_authorized' "
        "WHERE r.state='verifying_original' AND m.occurrence_id=? "
        "AND EXISTS(SELECT 1 FROM release_events d WHERE d.release_id=r.release_id "
        "AND d.kind='deployed')", (occurrence["id"],)).fetchall()
    for row in rows:
        proof = json.loads(row["details"])
        if (row["created"] > event["occurred"]
                or proof != {"occurrence_id": occurrence["id"], "flow": event["flow"],
                             "task_id": event["task_id"],
                             "workbench_version": event.get("workbench_version")}):
            continue
        try:
            require_source_authorization(db, event["flow"], event["task_id"])
        except Conflict:
            return False
        return True
    return False


@contextmanager
def existing_database(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise Conflict("release_database_required")
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def schema(db):
    return {row["name"]: [dict(column) for column in
            db.execute('PRAGMA table_info("' + row["name"].replace('"', '""') + '")')]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                  "AND name NOT LIKE 'sqlite_%'")}


def compatible(expected, actual):
    # New tables/nullable columns are safe; removed/retyped columns are not.
    for table, columns in expected.items():
        current = {item["name"]: item for item in actual.get(table, [])}
        for column in columns:
            if current.get(column["name"]) != column:
                return False
        for column in actual.get(table, []):
            if column["name"] not in {item["name"] for item in columns}:
                if column["notnull"] and column["dflt_value"] is None:
                    return False
    return True


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=True, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise Conflict("release_state_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Conflict("release_action_busy") from None
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "journal.db"

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,"
                       "release_id TEXT,kind TEXT,reason TEXT,created REAL,details TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS restarts(id TEXT PRIMARY KEY,created REAL)")
            yield db

    def event(self, release_id, kind, reason=None, *, event_key=None, **details):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                       (event_key or uuid4().hex, release_id, kind, reason,
                        time.time(), json.dumps(details)))

    def reserve_restart(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM restarts WHERE created>?",
                               (now - 600,)).fetchone()[0]
            if count >= 3:
                raise Conflict("release_restart_limit")
            db.execute("INSERT INTO restarts VALUES(?,?)", (uuid4().hex, now))

    def deliver(self, store):
        # The queue survives an unavailable application DB; stable IDs make replay harmless.
        with self.connection() as queue:
            events = queue.execute("SELECT * FROM events ORDER BY created LIMIT 200").fetchall()
        if not events:
            return
        from .fault_contract import FaultEvent
        from .system_faults import enqueue

        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for event in events:
                inserted = db.execute("INSERT OR IGNORE INTO release_events VALUES(?,?,?,?,?,?)",
                                      tuple(event)).rowcount
                if inserted and event["kind"] in {"blocked", "restart", "rollback", "failed"}:
                    enqueue(db, FaultEvent(
                        source_event_key="supervisor:" + event["id"], flow="service",
                        task_id=event["release_id"], source_table="release_events",
                        source_id=event["id"], occurred=event["created"], stage="supervisor",
                        code=event["reason"] or "service_restart", category="environment",
                        execution="executed", revision=event["details"] and
                        json.loads(event["details"]).get("commit"),
                        workbench_version=store.workbench_version,
                        diagnostic={"reason": event["reason"] or event["kind"]},
                    ))
        with self.connection() as queue:
            queue.executemany("DELETE FROM events WHERE id=?", [(item["id"],) for item in events])
