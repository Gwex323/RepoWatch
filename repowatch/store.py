import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import fixture

SDK_VERSION = "0.23.1"
GRAPH_VERSION = "fixture-v1"
FIXTURE_ISSUE_REF = "fixture://page-window/issues/1"


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class ValidationBlocked(Conflict):
    pass


def reproduction_evidence(run):
    """Authorize from current task evidence, never from memory or model text."""
    def decoded(key):
        value = run[key]
        return json.loads(value) if isinstance(value, str) else value

    if run["revision"] != fixture.REVISION:
        raise Conflict("The reproduction revision is no longer current.")
    if run["source_kind"] == "discovered":
        evidence = decoded("confirmation")
        if not evidence or evidence.get("decision") != "confirmed":
            raise Conflict("Human reproduction confirmation is required.")
        if evidence.get("actor") != "owner":
            raise Conflict("Owner confirmation is required.")
    elif run["source_kind"] == "existing_issue":
        evidence = decoded("automatic_validation")
        if (
            run["repo_id"] != "repo_fixture" or run["issue_ref"] != FIXTURE_ISSUE_REF
            or not evidence or evidence.get("decision") != "verified"
            or evidence.get("actor") != "workflow"
            or evidence.get("run_id") != run["id"]
            or evidence.get("repo_id") != run["repo_id"]
            or evidence.get("issue_ref") != run["issue_ref"]
        ):
            raise Conflict("Current automatic Issue validation is required.")
    else:
        raise Conflict("Unknown finding origin.")
    observation = evidence.get("observation")
    if (
        evidence.get("revision") != run["revision"]
        or not observation or observation.get("reproduced") is not True
        or observation.get("revision") != run["revision"]
    ):
        raise Conflict("Matching reproduction evidence is required.")
    return evidence


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def snapshot_digest(run_id: str, snapshot: str) -> str:
    return hashlib.sha256(f"{run_id}\0{snapshot}".encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT,
                    kind TEXT NOT NULL, language TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
                    status TEXT NOT NULL, agent TEXT, revision TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    snapshot TEXT, snapshot_hash TEXT, sdk_version TEXT NOT NULL,
                    graph_version TEXT NOT NULL, error TEXT, confirmation TEXT,
                    observation TEXT, note TEXT NOT NULL DEFAULT '',
                    result TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    lease REAL NOT NULL DEFAULT 0, owner TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id, kind)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    event_key TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT,
                    message TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(run_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(run_id, kind)
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO repositories VALUES (?, ?, NULL, ?, ?, 0, ?)",
                ("repo_fixture", "samples / page-window", "fixture", "Python", time.time()),
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for name, definition in (
                ("source_kind", "TEXT NOT NULL DEFAULT 'discovered' "
                 "CHECK(source_kind IN ('discovered','existing_issue'))"),
                ("issue_ref", "TEXT"),
                ("automatic_validation", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            db.execute("""
                CREATE TRIGGER IF NOT EXISTS immutable_run_origin
                BEFORE UPDATE OF source_kind,issue_ref ON runs
                WHEN NEW.source_kind IS NOT OLD.source_kind
                  OR NEW.issue_ref IS NOT OLD.issue_ref
                BEGIN
                    SELECT RAISE(ABORT, 'Finding origin is immutable');
                END;
            """)
            db.commit()
            from .memory import initialize

            initialize(db)
            from .github_accounts import initialize as initialize_github

            initialize_github(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def setting(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def setup_password(self, password: str):
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM settings WHERE key='password'").fetchone():
                raise Conflict("An owner is already configured.")
            db.execute(
                "INSERT INTO settings VALUES ('password', ?)",
                (json.dumps({"salt": salt, "digest": digest}),),
            )

    def check_password(self, password: str) -> bool:
        saved = self.setting("password")
        if not saved:
            return False
        data = json.loads(saved)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), data["salt"].encode(), 600_000
        ).hex()
        return secrets.compare_digest(actual, data["digest"])

    def create_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + 43200),
            )
        return token, csrf

    def session(self, token: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT csrf FROM sessions WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def logout(self, token: str):
        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def repositories(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM repositories ORDER BY created").fetchall()
            ]

    def add_repository(self, name: str, url: str):
        with self.connection() as db:
            existing = db.execute("SELECT * FROM repositories WHERE url=?", (url,)).fetchone()
            if existing:
                return dict(existing)
            repo_id = uid("repo")
            db.execute(
                "INSERT INTO repositories VALUES (?, ?, ?, 'github', 'Unknown', 0, ?)",
                (repo_id, name, url, time.time()),
            )
            return dict(db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone())

    def start_run(self, repo_id: str, *, reuse: bool = True) -> str:
        return self._start_fixture(repo_id, source_kind="discovered", reuse=reuse)

    def start_issue_sample(self, *, reuse: bool = True) -> str:
        return self._start_fixture("repo_fixture", source_kind="existing_issue", reuse=reuse)

    def _start_fixture(self, repo_id: str, *, source_kind: str, reuse: bool) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            repo = db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
            if not repo:
                raise Missing("Repository not found.")
            if repo["kind"] != "fixture":
                raise Conflict("External repository execution is not enabled in milestone one.")
            if reuse:
                row = db.execute(
                    "SELECT id FROM runs WHERE repo_id=? AND source_kind=? AND status IN "
                    "('queued','running','waiting','confirmed') ORDER BY created DESC LIMIT 1",
                    (repo_id, source_kind),
                ).fetchone()
                if row:
                    return row["id"]
            run_id, now = uid("run"), time.time()
            db.execute(
                "INSERT INTO runs (id,repo_id,status,revision,created,updated,sdk_version,"
                "graph_version,source_kind,issue_ref) VALUES (?,?,'queued',?,?,?,?,?,?,?)",
                (run_id, repo_id, fixture.REVISION, now, now, SDK_VERSION, GRAPH_VERSION,
                 source_kind, FIXTURE_ISSUE_REF if source_kind == "existing_issue" else None),
            )
            db.execute(
                "INSERT INTO jobs (id,run_id,kind) VALUES (?,?,'start')", (uid("job"), run_id)
            )
            return run_id

    def run(self, run_id: str, *, private=False):
        with self.connection() as db:
            row = db.execute(
                "SELECT runs.*, repositories.name AS repository FROM runs "
                "JOIN repositories ON repositories.id=runs.repo_id WHERE runs.id=?",
                (run_id,),
            ).fetchone()
            if not row:
                raise Missing("Run not found.")
            data = dict(row)
            data["has_snapshot"] = bool(data["snapshot"])
            if not private:
                data.pop("snapshot")
                data.pop("snapshot_hash")
            for key in ("observation", "confirmation", "result", "automatic_validation"):
                data[key] = json.loads(data[key]) if data[key] else None
            data["title"] = fixture.TITLE
            return data

    def dashboard(self):
        with self.connection() as db:
            ids = [
                r["id"] for r in db.execute("SELECT id FROM runs ORDER BY created DESC LIMIT 200")
            ]
            events = [
                dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            ]
        return {
            "repositories": self.repositories(),
            "runs": [self.run(i) for i in ids],
            "events": events,
        }

    def details(self, run_id: str):
        from .memory import Memory

        result = self.run(run_id)
        result["memory_reads"] = Memory(self).reads(run_id)
        with self.connection() as db:
            result["events"] = [
                dict(r)
                for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))
            ]
            result["artifacts"] = [
                {**dict(r), "content": json.loads(r["content"])}
                for r in db.execute(
                    "SELECT * FROM artifacts WHERE run_id=? ORDER BY created", (run_id,)
                )
            ]
        return result

    def event(self, run_id: str, key: str, kind: str, agent: str, message: str):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO events "
                "(run_id,event_key,kind,agent,message,created) VALUES (?,?,?,?,?,?)",
                (run_id, key, kind, agent, message, time.time()),
            )
            db.execute("UPDATE runs SET agent=?,updated=? WHERE id=?", (agent, time.time(), run_id))

    def set_running(self, run_id: str):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status='running',updated=?,error=NULL WHERE id=?",
                (time.time(), run_id),
            )

    def save_waiting(self, run_id: str, snapshot: str):
        if self.run(run_id)["source_kind"] != "discovered":
            raise Conflict("Existing Issues must not create a human reproduction checkpoint.")
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status='waiting',snapshot=?,snapshot_hash=?,updated=? WHERE id=?",
                (snapshot, snapshot_digest(run_id, snapshot), time.time(), run_id),
            )
        self.event(
            run_id,
            "checkpoint",
            "checkpoint",
            "reproduction_assistant",
            "Human reproduction required; SDK state saved.",
        )

    def observe(self, run_id: str, total: int, page: int, size: int):
        result = fixture.experiment(total, page, size)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise Missing("Run not found.")
            if (
                run["source_kind"] != "discovered" or run["status"] != "waiting"
                or run["revision"] != fixture.REVISION
            ):
                raise Conflict("This finding is not awaiting reproduction at this revision.")
            result["at"] = time.time()
            db.execute(
                "UPDATE runs SET observation=?,updated=? WHERE id=?",
                (json.dumps(result), time.time(), run_id),
            )
        return result

    def confirm(self, run_id: str, decision: str, revision: str, note: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise Missing("Run not found.")
            if run["source_kind"] != "discovered":
                raise Conflict("Existing Issues use automatic validation, not owner confirmation.")
            if run["revision"] != revision or revision != fixture.REVISION:
                raise Conflict("The finding revision changed. Reproduction must be repeated.")
            if run["confirmation"]:
                previous = json.loads(run["confirmation"])
                if previous["decision"] == decision:
                    return False
                raise Conflict("This finding already has a different recorded decision.")
            if run["status"] != "waiting" or not run["snapshot"]:
                raise Conflict("No pending human checkpoint exists.")
            observation = json.loads(run["observation"]) if run["observation"] else None
            if decision == "confirmed" and (
                not observation
                or not observation["reproduced"]
                or observation["revision"] != revision
            ):
                raise Conflict("Run the fixture personally and observe the discrepancy first.")
            confirmation = {
                "decision": decision,
                "revision": revision,
                "at": time.time(),
                "actor": "owner",
                "observation": observation,
            }
            status = "confirmed" if decision == "confirmed" else "not_reproduced"
            db.execute(
                "UPDATE runs SET status=?,confirmation=?,note=?,updated=? WHERE id=?",
                (status, json.dumps(confirmation), note, time.time(), run_id),
            )
            if decision == "confirmed":
                db.execute(
                    "INSERT OR IGNORE INTO jobs (id,run_id,kind) VALUES (?,?,'resume')",
                    (uid("job"), run_id),
                )
            db.execute(
                "INSERT INTO events (run_id,event_key,kind,agent,message,created) "
                "VALUES (?,'user-decision','human','owner',?,?)",
                (run_id, decision, time.time()),
            )
            from .memory import capture_reproduction

            capture_reproduction(db, run_id)
            return True

    def require_confirmation(self, run_id: str):
        run = self.run(run_id)
        if run["source_kind"] != "discovered":
            raise Conflict("This is not a human-confirmed discovery.")
        reproduction_evidence(run)
        return run

    def require_reproduction(self, run_id: str):
        run = self.run(run_id)
        reproduction_evidence(run)
        return run

    def validate_issue_sample(self, run_id: str):
        from .memory import capture_reproduction

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise Missing("Run not found.")
            if (
                run["source_kind"] != "existing_issue" or run["repo_id"] != "repo_fixture"
                or run["issue_ref"] != FIXTURE_ISSUE_REF or run["revision"] != fixture.REVISION
                or run["status"] != "running"
            ):
                raise Conflict("Automatic validation requires the trusted Issue sample.")
            observation = fixture.experiment(20, 3, 10)
            verified = (
                observation.get("reproduced") is True
                and observation.get("revision") == run["revision"]
            )
            evidence = {
                "decision": "verified" if verified else "failed", "actor": "workflow",
                "run_id": run_id, "repo_id": run["repo_id"], "issue_ref": run["issue_ref"],
                "revision": run["revision"], "observation": observation, "at": time.time(),
            }
            db.execute(
                "UPDATE runs SET automatic_validation=?,updated=? WHERE id=?",
                (json.dumps(evidence), time.time(), run_id),
            )
            if verified:
                capture_reproduction(db, run_id)
        if not verified:
            raise ValidationBlocked(
                "Automatic validation failed. Task paused; retry without human reproduction."
            )
        self.event(run_id, "automatic-validation", "validation", "reproduction_assistant",
                   "Existing Issue sample automatically reproduced at the task revision.")
        return evidence

    def require_verified_patch(self, run_id: str):
        self.require_reproduction(run_id)
        with self.connection() as db:
            artifacts = {
                row["kind"]: json.loads(row["content"])
                for row in db.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,))
            }
        verification = artifacts.get("verification", {})
        if (
            artifacts.get("patch_draft", {}).get("diff") != fixture.PATCH
            or verification.get("failed") != 0 or verification.get("passed", 0) < 1
        ):
            raise Conflict("A passing fixture patch review is required.")
        return artifacts

    def artifact(self, run_id: str, kind: str, content: dict):
        run = self.require_reproduction(run_id)
        if kind == "issue_draft" and run["source_kind"] != "discovered":
            raise Conflict("An existing Issue must not create a duplicate Issue draft.")
        if content.get("published") or content.get("merge_enabled"):
            raise Conflict("Publication and merge are disabled.")
        if kind == "pr_draft":
            self.require_verified_patch(run_id)
        with self.connection() as db:
            if kind == "verification":
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?) ON CONFLICT(run_id,kind) "
                    "DO UPDATE SET content=excluded.content,created=excluded.created",
                    (run_id, kind, json.dumps(content), time.time()),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?)",
                    (run_id, kind, json.dumps(content), time.time()),
                )

    def complete(self, run_id: str, result: str):
        artifacts = self.require_verified_patch(run_id)
        if not artifacts.get("pr_draft"):
            raise Conflict("A reviewed PR draft is required for completion.")
        with self.connection() as db:
            from .memory import capture_fix

            db.execute("BEGIN IMMEDIATE")
            capture_fix(db, run_id)
            db.execute(
                "UPDATE runs SET status='demo_completed',result=?,updated=? WHERE id=?",
                (json.dumps({"message": result, "published": False}), time.time(), run_id),
            )

    def fail(self, run_id: str, error: str):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status='failed',error=?,updated=? WHERE id=?",
                (error[:2000], time.time(), run_id),
            )

    def block_validation(self, run_id: str, error: str):
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status='validation_blocked',error=?,updated=? WHERE id=?",
                (error[:2000], time.time(), run_id),
            )
        self.event(run_id, "validation-blocked", "validation_blocked", "reproduction_assistant",
                   "Automatic validation paused; no human checkpoint was created.")

    def retry(self, run_id: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] not in ("failed", "validation_blocked"):
                raise Conflict("Only failed runs may be retried.")
            kind = "resume" if run["confirmation"] else "start"
            job = db.execute(
                "SELECT * FROM jobs WHERE run_id=? AND kind=?", (run_id, kind)
            ).fetchone()
            if not job or job["attempts"] >= 3:
                raise Conflict("This task has reached its retry limit.")
            db.execute(
                "UPDATE jobs SET status='queued',lease=0,owner=NULL WHERE id=?", (job["id"],)
            )
            db.execute("UPDATE runs SET status='queued',error=NULL WHERE id=?", (run_id,))

    def claim_job(self, owner: str):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' OR "
                "(status='running' AND lease<?) ORDER BY rowid LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running',lease=?,owner=?,attempts=attempts+1 WHERE id=?",
                (now + 30, owner, row["id"]),
            )
            return {**dict(row), "attempts": row["attempts"] + 1}

    def renew_job(self, job_id: str, owner: str):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET lease=? WHERE id=? AND owner=? AND status='running'",
                (time.time() + 30, job_id, owner),
            )

    def finish_job(self, job_id: str, owner: str, *, status="done"):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET status=?,lease=0 WHERE id=? AND owner=?", (status, job_id, owner)
            )
