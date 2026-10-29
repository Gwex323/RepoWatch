"""Per-repository daily check scheduling."""

import asyncio
import logging
import re
import time
from datetime import datetime

from .store import Conflict, Missing

LOG = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 60
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS repository_schedules (
            repo_id TEXT PRIMARY KEY REFERENCES repositories(id),
            enabled INTEGER NOT NULL DEFAULT 0,
            local_time TEXT NOT NULL DEFAULT '09:00',
            last_run_date TEXT NOT NULL DEFAULT '',
            last_status TEXT NOT NULL DEFAULT 'idle'
                CHECK(last_status IN ('idle','paused','scheduled','skipped_active','failed')),
            last_detail TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repository_schedule_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT NOT NULL REFERENCES repositories(id),
            run_date TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('scheduled','skipped_active','failed')),
            detail TEXT NOT NULL,
            environment_id TEXT NOT NULL DEFAULT '',
            pipeline_id TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            UNIQUE(repo_id, run_date)
        );
    """)


class RepositorySchedules:
    def __init__(self, store, execution):
        self.store, self.execution = store, execution
        self.closing = asyncio.Event()

    @staticmethod
    def _validate_time(local_time):
        if not isinstance(local_time, str) or not TIME_PATTERN.fullmatch(local_time):
            raise Conflict("schedule_time_invalid")

    def _repository(self, db, repo_id):
        row = db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
        if not row:
            raise Missing("Repository not found.")
        if row["kind"] != "github":
            raise Conflict("github_repository_required")
        return row

    def _ensure(self, db, repo_id):
        self._repository(db, repo_id)
        now = time.time()
        db.execute(
            "INSERT OR IGNORE INTO repository_schedules(repo_id,created,updated) "
            "VALUES(?,?,?)",
            (repo_id, now, now),
        )
        return db.execute(
            "SELECT * FROM repository_schedules WHERE repo_id=?", (repo_id,)
        ).fetchone()

    @staticmethod
    def _row(row):
        result = {
            key: row[key] for key in (
                "repo_id", "enabled", "local_time", "last_run_date", "last_status",
                "last_detail", "version", "created", "updated",
            )
        }
        result["enabled"] = bool(result["enabled"])
        return result

    def get(self, repo_id):
        with self.store.connection() as db:
            row = self._ensure(db, repo_id)
        return self._row(row)

    def list(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.id AS repo_id,r.name AS repository,r.kind,"
                "COALESCE(s.enabled,0) AS enabled,"
                "COALESCE(s.local_time,'09:00') AS local_time,"
                "COALESCE(s.last_run_date,'') AS last_run_date,"
                "COALESCE(s.last_status,'paused') AS last_status,"
                "COALESCE(s.last_detail,'') AS last_detail "
                "FROM repositories r "
                "LEFT JOIN repository_schedules s ON s.repo_id=r.id "
                "WHERE r.kind='github' ORDER BY r.created"
            ).fetchall()
        result = []
        for row in rows:
            item = {key: row[key] for key in (
                "repo_id", "repository", "kind", "enabled", "local_time",
                "last_run_date", "last_status", "last_detail",
            )}
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def summary(self):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total,SUM(enabled) AS enabled_count "
                "FROM repository_schedules s JOIN repositories r ON r.id=s.repo_id "
                "WHERE r.kind='github'"
            ).fetchone()
        enabled_count = row["enabled_count"] or 0
        return {
            "frequency": "daily", "enabled": enabled_count > 0,
            "repositories": row["total"], "enabled_repositories": enabled_count,
        }

    def configure(self, repo_id, *, enabled, local_time, version):
        self._validate_time(local_time)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._ensure(db, repo_id)
            if row["version"] != version:
                raise Conflict("schedule_changed")
            db.execute(
                "UPDATE repository_schedules SET enabled=?,local_time=?,last_status=?,"
                "version=version+1,updated=? WHERE repo_id=? AND version=?",
                (
                    int(enabled), local_time,
                    "paused" if not enabled else "idle", time.time(),
                    repo_id, version,
                ),
            )
            row = self._ensure(db, repo_id)
        return self._row(row)

    def _record(self, repo_id, status, detail, *, environment_id="", pipeline_id=""):
        with self.store.connection() as db:
            schedule = db.execute(
                "SELECT last_run_date FROM repository_schedules WHERE repo_id=?",
                (repo_id,),
            ).fetchone()
            db.execute(
                "UPDATE repository_schedules SET last_status=?,last_detail=?,"
                "version=version+1,updated=? WHERE repo_id=?",
                (status, str(detail)[:200], time.time(), repo_id),
            )
            if schedule and schedule["last_run_date"]:
                now = time.time()
                history_detail = (
                    "" if status in ("scheduled", "skipped_active") else str(detail)[:200]
                )
                db.execute(
                    "INSERT INTO repository_schedule_history"
                    "(repo_id,run_date,status,detail,environment_id,pipeline_id,created) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(repo_id,run_date) DO UPDATE SET "
                    "status=excluded.status,detail=excluded.detail,"
                    "environment_id=excluded.environment_id,"
                    "pipeline_id=excluded.pipeline_id,"
                    "created=repository_schedule_history.created",
                    (
                        repo_id, schedule["last_run_date"], status, history_detail,
                        environment_id, pipeline_id, now,
                    ),
                )

    def history(self, repo_id, *, limit=10):
        with self.store.connection() as db:
            self._repository(db, repo_id)
            rows = db.execute(
                "SELECT run_date,status,detail,environment_id,pipeline_id,created "
                "FROM repository_schedule_history WHERE repo_id=? "
                "ORDER BY created DESC,id DESC LIMIT ?",
                (repo_id, limit),
            ).fetchall()
        return [
            {
                "run_date": row["run_date"],
                "status": row["status"],
                "detail": row["detail"],
                "environment_id": row["environment_id"] or None,
                "pipeline_id": row["pipeline_id"] or None,
                "created": row["created"],
            }
            for row in rows
        ]

    def due(self, repo_id):
        schedule = self.get(repo_id)
        now = datetime.now()
        today, current_time = now.date().isoformat(), now.strftime("%H:%M")
        return (
            bool(schedule["enabled"]) and schedule["last_run_date"] != today
            and current_time >= schedule["local_time"]
        )

    def trigger(self, repo_id):
        with self.store.connection() as db:
            if self.store.draining(db):
                return None
        if not self.due(repo_id):
            return None
        now = datetime.now()
        today = now.date().isoformat()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._ensure(db, repo_id)
            if self.store.draining(db):
                return None
            if not row["enabled"] or row["last_run_date"] == today:
                return None
            changed = db.execute(
                "UPDATE repository_schedules SET last_run_date=?,last_status='scheduled',"
                "last_detail='',version=version+1,updated=? WHERE repo_id=? "
                "AND last_run_date<>? AND version=?",
                (today, time.time(), repo_id, today, row["version"]),
            ).rowcount
            if not changed:
                return None
            db.execute(
                "INSERT OR IGNORE INTO repository_schedule_history"
                "(repo_id,run_date,status,detail,environment_id,pipeline_id,created) "
                "VALUES(?,?,'scheduled','','','',?)",
                (repo_id, today, time.time()),
            )
        pipeline = self.execution.active_pipeline(repo_id)
        if pipeline:
            self._record(repo_id, "skipped_active", pipeline["id"], pipeline_id=pipeline["id"])
            return {"status": "skipped_active", "pipeline_id": pipeline["id"]}
        try:
            environment = self.execution.create(repo_id, auto_run=True)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, Conflict) else "schedule_execution_failed"
            self._record(repo_id, "failed", detail)
            LOG.warning("Daily check for %s failed: %s", repo_id, detail)
            return {"status": "failed", "detail": detail}
        self._record(
            repo_id, "scheduled", environment["id"], environment_id=environment["id"]
        )
        return {"status": "scheduled", "environment_id": environment["id"]}

    def tick(self):
        with self.store.connection() as db:
            repo_ids = [
                row["repo_id"] for row in db.execute(
                    "SELECT s.repo_id FROM repository_schedules s "
                    "JOIN repositories r ON r.id=s.repo_id "
                    "WHERE s.enabled=1 AND r.kind='github' ORDER BY r.created"
                )
            ]
        return [result for repo_id in repo_ids if (result := self.trigger(repo_id))]

    async def run(self):
        while not self.closing.is_set():
            try:
                self.tick()
            except Exception:
                from .system_faults import record_worker_failure

                record_worker_failure(self.store, "repository_schedule_worker")
                LOG.exception("Repository schedule worker failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
