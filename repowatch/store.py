import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SDK_VERSION = "0.23.1"


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.faults_ready = False
        self.fault_ingestion_error = None
        self.fault_capture_failures = 0
        # A source digest identifies the code actually loaded, including a local build.
        source = Path(__file__).resolve().parent
        self.workbench_version = hashlib.sha256(b"".join(
            item.name.encode() + item.read_bytes() for item in sorted(source.glob("*.py"))
        )).hexdigest()[:32]
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
            """)
            from .legacy_samples import retire_samples

            retire_samples(db)
            from .memory import initialize

            initialize(db)
            from .github_accounts import initialize as initialize_github

            initialize_github(db)
            from .github_issues import initialize as initialize_issues

            initialize_issues(db)
            from .schedules import initialize as initialize_schedules

            initialize_schedules(db)
            db.commit()
            from .execution import initialize as initialize_execution

            initialize_execution(db)
            db.commit()
            from .analysis_model import initialize as initialize_model
            from .repository_analysis import initialize as initialize_analysis

            initialize_analysis(db)
            initialize_model(db)
            from .model_providers import initialize as initialize_providers

            initialize_providers(db)
            from .memory_evaluation import initialize as initialize_memory_evaluation

            initialize_memory_evaluation(db)
            from .repairs import initialize as initialize_repair

            initialize_repair(db)
            from .task_scopes import initialize as initialize_scopes

            initialize_scopes(db)
            from .system_faults import initialize as initialize_faults

            initialize_faults(db)
            from .recovery import initialize as initialize_recovery

            initialize_recovery(db)
            from .coordination import initialize as initialize_coordination

            initialize_coordination(db)
            from .maintenance import initialize as initialize_maintenance

            initialize_maintenance(db)
            from .release_state import initialize as initialize_release

            initialize_release(db)
            health = db.execute("SELECT failures FROM system_fault_ingestion "
                                "WHERE source='source_capture'").fetchone()
            if health:
                self.fault_capture_failures = health[0]
        self.faults_ready = True

    @staticmethod
    def draining(db):
        from .release_state import draining

        return draining(db)

    @contextmanager
    def connection(self):
        fault_transaction_started = time.time()
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            if self.faults_ready and db.total_changes:
                from .system_faults import capture_safely

                capture_safely(self, db, live_after=fault_transaction_started)
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
                for row in db.execute(
                    "SELECT * FROM repositories WHERE kind='github' ORDER BY created",
                ).fetchall()
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

    def dashboard(self):
        from .dashboard import snapshot

        with self.connection() as db:
            return snapshot(db)
