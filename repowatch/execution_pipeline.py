"""Owner-authorized, durable download -> setup -> test -> start -> analysis chain."""

import json
import re
import time

import httpx

from .desktop_projects import desktop_guard
from .diagnosis import CONFIG_KEYS, fingerprint
from .repository_source import discover
from .store import Conflict, uid

ACTIVE = (
    "queued",
    "running",
    "waiting_configuration",
    "waiting_environment",
    "waiting_analysis",
    "waiting_model",
)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS execution_pipelines (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, env_id TEXT NOT NULL,
            account_id INTEGER NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
            config_hash TEXT, error TEXT, analysis_id TEXT,
            next_check REAL NOT NULL DEFAULT 0, checks INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_repository_pipeline
            ON execution_pipelines(repo_id) WHERE state IN
            ('queued','running','waiting_configuration','waiting_environment',
             'waiting_analysis','waiting_model');
        CREATE TABLE IF NOT EXISTS repository_execution_preferences (
            repo_id TEXT NOT NULL, account_id INTEGER NOT NULL, overrides TEXT NOT NULL,
            language TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(repo_id,account_id)
        );
    """)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(execution_jobs)")}
    if "pipeline_id" not in columns:
        db.execute("ALTER TABLE execution_jobs ADD COLUMN pipeline_id TEXT")


def passing_test(env):
    result = json.loads(env["test_result"] or "null")
    config = json.loads(env["config"] or "{}")
    if config.get("project_profile", {}).get("kind") == "python_desktop" and (
        not config.get("desktop_approved") or not result
        or not result.get("desktop_scope", {}).get("executed")
    ):
        return False
    return bool(
        env["installed"]
        and result
        and result.get("exit_code") == 0
        and result.get("revision") == env["revision"]
        and result.get("command") == config.get("test")
        and config.get("test", "").strip()
    )


def empty_tests(log):
    return bool(
        re.search(
            r"(?im)(?:\bRan\s+0\s+tests?\b|\bno tests (?:ran|collected|found)\b|"
            r"^#\s*tests\s+0\b|\b0 passing\b)",
            log,
        )
        or ("[no test files]" in log and not re.search(r"(?m)^ok\s", log))
    )


class ExecutionPipeline:
    def _save_preferences(self, db, env, config):
        detected = discover(self.directory(env["id"]) / "source")
        overrides = {
            key: config[key]
            for key in (*CONFIG_KEYS, "run_mode")
            if key in config
            and config[key] != detected.get(key, "service" if key == "run_mode" else None)
        }
        if config.get("project_profile", {}).get("kind") == "python_desktop":
            overrides.update(run_mode=config["run_mode"], desktop_approved=True)
        db.execute(
            "INSERT INTO repository_execution_preferences VALUES(?,?,?,?,?) "
            "ON CONFLICT(repo_id,account_id) DO UPDATE SET overrides=excluded.overrides,"
            "language=excluded.language,updated=excluded.updated",
            (
                env["repo_id"],
                env["account_id"],
                json.dumps(overrides),
                config["language"],
                time.time(),
            ),
        )

    def _preferred_configuration(self, env, config):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM repository_execution_preferences WHERE repo_id=? AND account_id=?",
                (env["repo_id"], env["account_id"]),
            ).fetchone()
        if row and row["language"] == config.get("language"):
            overrides = json.loads(row["overrides"])
            desktop = config.get("project_profile", {}).get("kind") == "python_desktop"
            if desktop != bool(overrides.get("desktop_approved")):
                return config
            return {
                **config,
                **overrides,
                "detected": config["detected"],
                "reason": "saved_owner_configuration",
            }
        return config

    @staticmethod
    def _active_pipeline(db, *, env_id=None, repo_id=None):
        column, value = ("env_id", env_id) if env_id else ("repo_id", repo_id)
        return db.execute(
            f"SELECT * FROM execution_pipelines WHERE {column}=? AND state IN "
            "('queued','running','waiting_configuration','waiting_environment',"
            "'waiting_analysis','waiting_model') ORDER BY created DESC LIMIT 1",
            (value,),
        ).fetchone()

    @staticmethod
    def _pipeline_update(db, pipeline_id, **values):
        db.execute(
            "UPDATE execution_pipelines SET "
            + ",".join(f"{key}=?" for key in values)
            + ",version=version+1,updated=? WHERE id=?",
            (*values.values(), time.time(), pipeline_id),
        )

    def _new_pipeline(self, db, env, stage):
        now, pipeline_id = time.time(), uid("pipeline")
        db.execute(
            "INSERT INTO execution_pipelines(id,repo_id,env_id,account_id,state,stage,created,"
            "updated,config_hash) VALUES(?,?,?,?,'queued',?,?,?,?)",
            (
                pipeline_id,
                env["repo_id"],
                env["id"],
                env["account_id"],
                stage,
                now,
                now,
                fingerprint(json.loads(env["config"] or "{}"), env["revision"])
                if env["revision"]
                else None,
            ),
        )
        return db.execute("SELECT * FROM execution_pipelines WHERE id=?", (pipeline_id,)).fetchone()

    def _queue_pipeline_stage(self, db, pipeline, action):
        env = self.row(db, pipeline["env_id"])
        config = json.loads(env["config"] or "{}")
        account = db.execute(
            "SELECT a.id FROM github_accounts a JOIN github_configuration c "
            "ON c.active_account_id=a.id WHERE a.state='verified'"
        ).fetchone()
        if not account or account["id"] != pipeline["account_id"]:
            self._pipeline_update(
                db, pipeline["id"], state="failed", error="pipeline_account_changed"
            )
            return
        desktop_problem = desktop_guard(config)
        if desktop_problem:
            self._pipeline_update(db, pipeline["id"], state="waiting_configuration",
                                  stage=action, error=desktop_problem)
            return
        if action == "setup" and not config.get("detected"):
            self._pipeline_update(
                db,
                pipeline["id"],
                state="waiting_configuration",
                stage=action,
                error="execution_config_required",
            )
            return
        if action == "test" and not config.get("test", "").strip():
            self._pipeline_update(
                db,
                pipeline["id"],
                state="waiting_configuration",
                stage=action,
                error="pipeline_test_command_required",
            )
            return
        if action == "start":
            if not passing_test(env):
                self._pipeline_update(
                    db, pipeline["id"], state="failed", error="pipeline_test_required"
                )
                return
            if config.get("run_mode", "service") == "test_only":
                self._pipeline_update(
                    db,
                    pipeline["id"],
                    state="waiting_analysis",
                    stage="analysis",
                    error=None,
                    config_hash=fingerprint(config, env["revision"]),
                )
                return
            if not config.get("start", "").strip():
                self._pipeline_update(
                    db,
                    pipeline["id"],
                    state="waiting_configuration",
                    stage=action,
                    error="pipeline_start_command_required",
                )
                return
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE env_id=? AND state IN ('queued','running')",
            (env["id"],),
        ).fetchone():
            raise Conflict("environment_busy")
        self.enqueue(db, env["id"], action, pipeline_id=pipeline["id"])
        self._pipeline_update(
            db,
            pipeline["id"],
            state="running",
            stage=action,
            error=None,
            config_hash=fingerprint(config, env["revision"]),
            next_check=0,
        )
        db.execute(
            "UPDATE environments SET version=version+1,updated=? WHERE id=?",
            (time.time(), env["id"]),
        )

    def _pipeline_configured(self, db, env_id):
        pipeline = self._active_pipeline(db, env_id=env_id)
        if not pipeline:
            return
        if pipeline["state"] not in ("waiting_configuration", "waiting_environment"):
            self._cancel_pipeline(db, env_id, error="pipeline_configuration_changed")
            return
        env = self.row(db, env_id)
        stage = "setup" if not env["installed"] else "start" if passing_test(env) else "test"
        self._queue_pipeline_stage(db, pipeline, stage)

    def _begin_pipeline(self, db, env):
        active = self._active_pipeline(db, repo_id=env["repo_id"])
        if active:
            if active["env_id"] != env["id"]:
                raise Conflict("pipeline_busy")
            return
        pipeline = self._new_pipeline(db, env, "setup")
        if env["state"] == "running" and passing_test(env):
            stage = "access"
        else:
            stage = ("setup" if not env["installed"] else
                     "start" if passing_test(env) else "test")
        self._queue_pipeline_stage(db, pipeline, stage)

    def continue_pipeline(self, env_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.row(db, env_id)
            existing = self._active_pipeline(db, env_id=env_id)
            if existing:
                return self.get(env_id)
            if env["version"] != version:
                raise Conflict("environment_changed")
            self.editable(db, env_id, version)
            if not env["revision"] or env["state"] == "deleted" or env.get("reused_environment_id"):
                raise Conflict("environment_not_ready")
            self._begin_pipeline(db, env)
        return self.get(env_id)

    def _cancel_pipeline(self, db, env_id, *, error="execution_cancelled"):
        active_analyses = db.execute(
            "SELECT id FROM repository_analyses WHERE env_id=? "
            "AND status IN ('queued','running','waiting_model')",
            (env_id,),
        ).fetchall()
        self._cancel_analyses(db, [row["id"] for row in active_analyses])
        pipeline = self._active_pipeline(db, env_id=env_id)
        if not pipeline:
            return
        self._pipeline_update(db, pipeline["id"], state="cancelled", error=error)
        db.execute(
            "UPDATE execution_jobs SET cancel=1 WHERE pipeline_id=? "
            "AND state IN ('queued','running')",
            (pipeline["id"],),
        )

    def _cancel_analyses(self, db, analysis_ids):
        if not analysis_ids:
            return
        now = time.time()
        marks = ",".join("?" * len(analysis_ids))
        analysis_ids = [row["id"] for row in db.execute(
            f"SELECT id FROM repository_analyses WHERE id IN ({marks}) "
            "AND status IN ('queued','running','waiting_model')", analysis_ids,
        )]
        if not analysis_ids:
            return
        marks = ",".join("?" * len(analysis_ids))
        db.execute(
            f"UPDATE repository_analyses SET status='cancelled',error='analysis_cancelled',"
            f"lease=0,version=version+1,updated=? WHERE id IN ({marks})",
            (now, *analysis_ids),
        )
        db.execute(
            f"UPDATE analysis_report_recovery SET state='stopped',"
            f"stop_reason='pipeline_stopped',last_error='analysis_cancelled',retry_after=0 "
            f"WHERE analysis_id IN ({marks})",
            analysis_ids,
        )
        db.executemany(
            "INSERT INTO analysis_events(analysis_id,kind,agent,created) VALUES(?,?,?,?)",
            [(analysis_id, "analysis_cancelled", "pipeline_stopped", now)
             for analysis_id in analysis_ids],
        )

    def _pipeline_finished_job(self, db, job, error):
        if not job.get("pipeline_id"):
            return
        pipeline = db.execute(
            "SELECT * FROM execution_pipelines WHERE id=?", (job["pipeline_id"],)
        ).fetchone()
        if not pipeline or pipeline["state"] not in ACTIVE:
            return
        if error:
            if error == "docker_unavailable" and job.get("preflight_failed"):
                self._pipeline_update(
                    db,
                    pipeline["id"],
                    state="waiting_environment",
                    stage=job["action"],
                    error=error,
                    next_check=time.time() + 5,
                )
            else:
                state = "cancelled" if error == "execution_cancelled" else "failed"
                self._pipeline_update(db, pipeline["id"], state=state, error=error)
            return
        env = self.row(db, pipeline["env_id"])
        if job["action"] == "download":
            if env.get("reused_environment_id"):
                target = self.row(db, env["reused_environment_id"])
                if (
                    target["repo_id"] != pipeline["repo_id"]
                    or target["account_id"] != pipeline["account_id"]
                ):
                    raise Conflict("pipeline_account_changed")
                self._pipeline_update(db, pipeline["id"], env_id=target["id"])
                pipeline = db.execute(
                    "SELECT * FROM execution_pipelines WHERE id=?", (pipeline["id"],)
                ).fetchone()
                env = target
            self._queue_pipeline_stage(db, pipeline, "test" if env["installed"] else "setup")
        elif job["action"] == "setup":
            self._queue_pipeline_stage(db, pipeline, "test")
        elif job["action"] == "test":
            self._queue_pipeline_stage(db, pipeline, "start")
        elif job["action"] in ("start", "access"):
            self._pipeline_update(
                db, pipeline["id"], state="waiting_analysis", stage="analysis", error=None
            )

    def _pipeline_checkpoint(self, db, job):
        if not job.get("pipeline_id"):
            return
        pipeline = db.execute(
            "SELECT * FROM execution_pipelines WHERE id=?", (job["pipeline_id"],)
        ).fetchone()
        if not pipeline or pipeline["state"] not in ACTIVE:
            raise Conflict("execution_cancelled")
        env = self.row(db, job["env_id"])
        if job["action"] != "download" and pipeline["config_hash"] != fingerprint(
            json.loads(env["config"] or "{}"),
            env["revision"],
        ):
            raise Conflict("pipeline_configuration_changed")
        active = db.execute(
            "SELECT a.id FROM github_configuration c JOIN github_accounts a "
            "ON a.id=c.active_account_id WHERE c.id=1 AND a.state='verified'"
        ).fetchone()
        if not active or active[0] != pipeline["account_id"]:
            raise Conflict("pipeline_account_changed")

    def reconcile_pipelines(self):
        with self.store.connection() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM execution_pipelines WHERE state IN "
                    "('waiting_environment','waiting_analysis','waiting_model') "
                    "ORDER BY created LIMIT 20",
                )
            ]
        for pipeline in rows:
            if self.closing.is_set():
                return
            if pipeline["state"] == "waiting_environment":
                if pipeline["next_check"] > time.time():
                    continue
                ready = self.runtime.readiness()
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT * FROM execution_pipelines WHERE id=?", (pipeline["id"],)
                    ).fetchone()
                    if current["version"] != pipeline["version"] or current["state"] not in ACTIVE:
                        continue
                    if ready["ready"]:
                        self._queue_pipeline_stage(db, current, current["stage"])
                    else:
                        checks = min(pipeline["checks"] + 1, 6)
                        self._pipeline_update(
                            db,
                            pipeline["id"],
                            checks=checks,
                            next_check=time.time() + min(5 * 2**checks, 60),
                        )
                continue
            analyses = getattr(self, "analyses", None)
            if analyses is None:
                continue
            try:
                analysis_id = pipeline["analysis_id"] or analyses.enqueue(
                    pipeline["env_id"],
                    pipeline_id=pipeline["id"],
                )
            except Conflict as exc:
                if str(exc) in ("environment_busy", "analysis_busy"):
                    continue
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT state FROM execution_pipelines WHERE id=?",
                                         (pipeline["id"],)).fetchone()
                    if current and current["state"] in ACTIVE:
                        self._pipeline_update(db, pipeline["id"], state="failed", error=str(exc))
                continue
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT * FROM execution_pipelines WHERE id=?", (pipeline["id"],)
                ).fetchone()
                if current["state"] not in ACTIVE:
                    self._cancel_analyses(db, [analysis_id])
                    continue
                analysis = db.execute(
                    "SELECT status,error FROM repository_analyses WHERE id=?", (analysis_id,)
                ).fetchone()
                state = {
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "failed",
                    "cancelled": "cancelled",
                    "waiting_model": "waiting_model",
                }.get(
                    analysis["status"],
                    "waiting_analysis",
                )
                self._pipeline_update(
                    db,
                    pipeline["id"],
                    analysis_id=analysis_id,
                    state=state,
                    error=analysis["error"],
                )

    @staticmethod
    def probe_access(port):
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        base = f"http://127.0.0.2:{port}"
        with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
            for path in ("/", "/health", "/healthz", "/docs"):
                try:
                    with client.stream("GET", base + path) as response:
                        status = response.status_code
                        if 200 <= status < 400 or status in (401, 403):
                            return {
                                "status_code": status,
                                "path": path,
                                "checked_at": time.time(),
                                "authentication_required": status in (401, 403),
                            }
                except httpx.HTTPError:
                    continue
        return None
