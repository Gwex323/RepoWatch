"""Durable repository environments. Execution results never authorize a finding."""

import asyncio
import json
import logging
import re
import shutil
import socket
import threading
import time

from .desktop_projects import desktop_guard, offline_test_scope
from .diagnosis import CONFIG_KEYS, Diagnoses, Patch, fingerprint, manifest_context
from .docker_runtime import LOG_LIMIT, DockerRuntime
from .environment_reuse import EnvironmentReuse
from .environment_reuse import initialize as initialize_reuse
from .execution_pipeline import ExecutionPipeline, empty_tests
from .execution_pipeline import initialize as initialize_pipeline
from .github_accounts import GitHubError
from .repository_source import IMAGES, ExecutionError, RepositorySource, discover
from .store import Conflict, Missing, uid


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS environments (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
            account_id INTEGER NOT NULL, account_version INTEGER NOT NULL,
            revision TEXT, config TEXT, image_id TEXT,
            state TEXT NOT NULL DEFAULT 'queued', installed INTEGER NOT NULL DEFAULT 0,
            dependency_digest TEXT, cache_source_env_id TEXT,
            test_result TEXT, error TEXT, port INTEGER, expires REAL,
            version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_jobs (
            id TEXT PRIMARY KEY, env_id TEXT NOT NULL REFERENCES environments(id),
            action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            owner TEXT, lease REAL NOT NULL DEFAULT 0, cancel INTEGER NOT NULL DEFAULT 0,
            log TEXT NOT NULL DEFAULT '', error TEXT, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_environment_operation
            ON execution_jobs(env_id) WHERE state IN ('queued','running');
        CREATE TABLE IF NOT EXISTS execution_diagnoses (
            job_id TEXT PRIMARY KEY REFERENCES execution_jobs(id),
            env_id TEXT NOT NULL REFERENCES environments(id),
            revision TEXT, fingerprint TEXT NOT NULL, context TEXT NOT NULL,
            report TEXT NOT NULL, state TEXT NOT NULL, lease REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL
        );
    """)
    initialize_reuse(db)
    initialize_pipeline(db)
    env_columns = {row["name"] for row in db.execute("PRAGMA table_info(environments)")}
    for name, definition in (
        ("dependency_digest", "TEXT"), ("cache_source_env_id", "TEXT"),
        ("reused_environment_id", "TEXT"), ("preparation", "TEXT"),
        ("access_result", "TEXT"),
    ):
        if name not in env_columns:
            db.execute(f"ALTER TABLE environments ADD COLUMN {name} {definition}")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(execution_jobs)")}
    if "context" not in columns:
        db.execute("ALTER TABLE execution_jobs ADD COLUMN context TEXT")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('execution_namespace', ?)", (uid("local"),))


class Execution(EnvironmentReuse, ExecutionPipeline):
    def __init__(self, store, github, *, runtime=None, source=None, diagnoser=None):
        self.store, self.github = store, github
        self.runtime = runtime or DockerRuntime(store.setting("execution_namespace"))
        self.source = source or RepositorySource(github)
        self.root = (store.path.parent / (store.path.stem + "-workspaces")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = uid("worker")
        self.closing = threading.Event()
        self.diagnoses = Diagnoses(self, diagnoser)

    def directory(self, env_id):
        if not re.fullmatch(r"env_[a-f0-9]{16}", env_id):
            raise Missing("Environment not found.")
        path = self.root / env_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ExecutionError("unsafe_workspace")
        return path

    def repository(self, db, repo_id):
        repo = db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
        if not repo:
            raise Missing("Repository not found.")
        if repo["kind"] != "github":
            raise Conflict("github_repository_required")
        if repo["url"] != "https://github.com/" + repo["name"] or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo["name"]
        ) or repo["name"].split("/")[-1] in (".", ".."):
            raise Conflict("github_repository_required")
        return dict(repo)

    @staticmethod
    def row(db, env_id):
        row = db.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
        if not row:
            raise Missing("Environment not found.")
        return dict(row)

    @staticmethod
    def unpack(row):
        for key in ("config", "test_result", "access_result"):
            row[key] = json.loads(row[key]) if row[key] else None
        row["installed"] = bool(row["installed"])
        row["url"] = f"http://127.0.0.2:{row['port']}" if row["state"] == "running" else None
        return row

    def get(self, env_id):
        with self.store.connection() as db:
            result = self.unpack(self.row(db, env_id))
            result["jobs"] = [dict(j) for j in db.execute(
                "SELECT id,action,state,cancel,log,error,created,updated FROM execution_jobs "
                "WHERE env_id=? ORDER BY created DESC LIMIT 20", (env_id,)
            )]
            pipeline = db.execute(
                "SELECT * FROM execution_pipelines WHERE env_id=? ORDER BY created DESC LIMIT 1",
                (env_id,),
            ).fetchone()
            result["pipeline"] = dict(pipeline) if pipeline else None
        result["busy"] = any(j["state"] in ("queued", "running") for j in result["jobs"])
        result["diagnosis"] = self.diagnoses.latest(result)
        return result

    def list(self, repo_id):
        with self.store.connection() as db:
            self.repository(db, repo_id)
            rows = db.execute(
                "SELECT e.*,EXISTS(SELECT 1 FROM execution_jobs j WHERE j.env_id=e.id "
                "AND j.state IN ('queued','running')) AS busy "
                "FROM environments e WHERE repo_id=? ORDER BY created DESC LIMIT 30",
                (repo_id,),
            ).fetchall()
        return [{**self.unpack(dict(row)), "jobs": []} for row in rows]

    @staticmethod
    def enqueue(db, env_id, action, *, pipeline_id=None):
        now = time.time()
        env = Execution.row(db, env_id)
        context = json.dumps({
            "revision": env["revision"], "config": json.loads(env["config"] or "{}"),
        })
        db.execute(
            "INSERT INTO execution_jobs(id,env_id,action,created,updated,context,pipeline_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid("exec"), env_id, action, now, now, context, pipeline_id),
        )

    def create(self, repo_id, *, auto_run=False):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository(db, repo_id)
            pipeline = self._active_pipeline(db, repo_id=repo_id)
            if pipeline:
                return self.get(pipeline["env_id"])
            existing = db.execute(
                "SELECT e.id FROM environments e JOIN execution_jobs j ON j.env_id=e.id "
                "WHERE e.repo_id=? AND j.state IN ('queued','running')", (repo_id,),
            ).fetchone()
            if existing:
                return self.get(existing["id"])
            account = db.execute(
                "SELECT a.id,a.version FROM github_accounts a JOIN github_configuration c "
                "ON c.active_account_id=a.id WHERE a.state='verified'"
            ).fetchone()
            if not account:
                raise Conflict("github_active_account_required")
            env_id, now = uid("env"), time.time()
            db.execute(
                "INSERT INTO environments(id,repo_id,account_id,account_version,created,updated) "
                "VALUES(?,?,?,?,?,?)",
                (env_id, repo_id, account["id"], account["version"], now, now),
            )
            if auto_run:
                pipeline = self._new_pipeline(db, self.row(db, env_id), "download")
                self.enqueue(db, env_id, "download", pipeline_id=pipeline["id"])
            else:
                self.enqueue(db, env_id, "download")
        return self.get(env_id)

    @staticmethod
    def editable(db, env_id, version):
        env = Execution.row(db, env_id)
        if env["version"] != version:
            raise Conflict("environment_changed")
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE env_id=? AND state IN ('queued','running')",
            (env_id,),
        ).fetchone():
            raise Conflict("environment_busy")
        return env

    @staticmethod
    def validate_config(config):
        mode = config.get("run_mode", "service")
        config = {key: config[key] for key in CONFIG_KEYS}
        config["run_mode"] = mode
        if (
            config["language"] not in IMAGES or config["run_mode"] not in ("service", "test_only")
            or type(config["port"]) is not int
            or not 1024 <= config["port"] <= 65535 or type(config["install_network"]) is not bool
            or any(not isinstance(config[k], str) or len(config[k]) > 4000
                   or "\x00" in config[k] for k in ("install", "test", "start"))
        ):
            raise Conflict("invalid_execution_config")
        return config

    def detect_configuration(self, env_id, version):
        proposal = discover(self.directory(env_id) / "source")
        if proposal.get("project_profile", {}).get("kind") != "python_desktop":
            raise Conflict("desktop_detection_unavailable")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.editable(db, env_id, version)
            previous = json.loads(env["config"] or "{}")
            if (not env["revision"] or env["installed"] or env["state"] == "deleted"
                    or any(previous.get(key, "").strip() for key in ("install", "test", "start"))):
                raise Conflict("configuration_detection_not_applicable")
            proposal["install_network"] = previous.get("install_network", False)
            db.execute("UPDATE environments SET config=?,state='config_required',error=NULL,"
                       "version=version+1,updated=? WHERE id=?",
                       (json.dumps(proposal, ensure_ascii=False), time.time(), env_id))
            self._pipeline_configured(db, env_id)
        return self.get(env_id)

    def configure(self, env_id, version, config, *, continue_pipeline=False,
                  desktop_offline_confirmed=False):
        config = self.validate_config(config)
        profile = discover(self.directory(env_id) / "source").get("project_profile")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.editable(db, env_id, version)
            if not env["revision"] or env["state"] in ("running", "deleted"):
                raise Conflict("environment_not_configurable")
            previous = json.loads(env["config"] or "{}")
            profile = profile or previous.get("project_profile")
            if profile:
                if config["language"] != "python" or config["run_mode"] != "test_only":
                    raise Conflict("desktop_test_only_required")
                if not (desktop_offline_confirmed or previous.get("desktop_approved")):
                    raise Conflict("desktop_confirmation_required")
                config.update(project_profile=profile, desktop_approved=True)
                problem = desktop_guard(config)
                if problem:
                    raise Conflict(problem)
            reinstall = any(previous.get(key) != config[key]
                            for key in ("language", "install", "install_network"))
            retest = (reinstall or previous.get("test") != config["test"] or
                      previous.get("project_profile", {}).get("kind") !=
                      config.get("project_profile", {}).get("kind"))
            installed = bool(env["installed"] and not reinstall)
            test_result = None if retest else env["test_result"]
            state = (env["state"] if installed and not retest else
                     "ready" if installed else "downloaded")
            error = env["error"] if state == "test_failed" and test_result else None
            db.execute(
                "UPDATE environments SET config=?,state=?,installed=?,test_result=?,image_id=?,"
                "access_result=NULL,error=?,version=version+1,updated=? WHERE id=?",
                (json.dumps({**config, "detected": True, "reason": "owner_override"}), state,
                 int(installed), test_result, env["image_id"] if installed else None,
                 error, time.time(), env_id),
            )
            self._save_preferences(db, env, config)
            self._pipeline_configured(db, env_id)
            if continue_pipeline:
                self._begin_pipeline(db, self.row(db, env_id))
        return self.get(env_id)

    def apply_diagnosis(self, env_id, job_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.editable(db, env_id, version)
            diagnosis = db.execute(
                "SELECT * FROM execution_diagnoses WHERE job_id=? AND env_id=?",
                (job_id, env_id),
            ).fetchone()
            latest = db.execute(
                "SELECT id FROM execution_jobs WHERE env_id=? ORDER BY created DESC LIMIT 1",
                (env_id,),
            ).fetchone()
            config = json.loads(env["config"] or "{}")
            if (
                not diagnosis or not latest or latest["id"] != job_id
                or diagnosis["state"] not in ("complete", "failed")
                or env["state"] not in ("setup_failed", "test_failed", "failed")
                or env["error"] == "cleanup_pending"
                or diagnosis["fingerprint"] != fingerprint(config, env["revision"])
            ):
                raise Conflict("diagnosis_stale")
            patch = Patch.model_validate(json.loads(diagnosis["report"])["patch"])
            changes = patch.model_dump(exclude_none=True)
            if not changes:
                raise Conflict("diagnosis_no_patch")
            metadata = {key: config[key] for key in ("project_profile", "desktop_approved")
                        if key in config}
            config = {**self.validate_config({**config, **changes}), **metadata}
            problem = desktop_guard(config)
            if problem:
                raise Conflict(problem)
            db.execute(
                "UPDATE environments SET config=?,state='downloaded',installed=0,test_result=NULL,"
                "image_id=NULL,error=NULL,version=version+1,updated=? WHERE id=?",
                (json.dumps({**config, "detected": True, "reason": "diagnosis_applied"}),
                 time.time(), env_id),
            )
            self.enqueue(db, env_id, "setup")
        return self.get(env_id)

    def action(self, env_id, version, action):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.row(db, env_id)
            if env["version"] != version:
                raise Conflict("environment_changed")
            pending = db.execute(
                "SELECT * FROM execution_jobs WHERE env_id=? AND state IN ('queued','running')",
                (env_id,),
            ).fetchone()
            if action in ("stop", "delete"):
                self._cancel_pipeline(db, env_id)
            elif self._active_pipeline(db, env_id=env_id):
                raise Conflict("pipeline_busy")
            if action == "stop" and pending:
                db.execute("UPDATE execution_jobs SET cancel=1 WHERE id=?", (pending["id"],))
                return {"accepted": True}
            self.editable(db, env_id, version)
            if env["state"] == "deleted":
                raise Conflict("environment_deleted")
            if env.get("reused_environment_id") and action not in ("delete", "stop"):
                raise Conflict("environment_reused")
            if action in ("setup", "test", "start"):
                if not env["revision"] or env["state"] == "running":
                    raise Conflict("environment_not_ready")
                config = json.loads(env["config"])
                problem = desktop_guard(config)
                if problem:
                    raise Conflict(problem)
                if action == "start" and config.get("project_profile", {}).get("kind"):
                    raise Conflict("desktop_service_unsupported")
                if not config["detected"]:
                    raise Conflict("execution_config_required")
                if action != "setup" and not env["installed"]:
                    raise Conflict("setup_required")
                if action != "setup" and not config[action].strip():
                    raise Conflict("execution_command_required")
            if action not in ("setup", "test", "start", "stop", "delete"):
                raise Conflict("invalid_execution_action")
            self.enqueue(db, env_id, action)
            db.execute("UPDATE environments SET version=version+1,updated=? WHERE id=?",
                       (time.time(), env_id))
        return self.get(env_id)

    def update(self, env_id, **values):
        with self.store.connection() as db:
            db.execute(
                "UPDATE environments SET " + ",".join(f"{k}=?" for k in values)
                + ",updated=?,version=version+1 WHERE id=?",
                (*values.values(), time.time(), env_id),
            )

    def claim(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM execution_jobs WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE execution_jobs SET state='running',owner=?,lease=?,updated=? WHERE id=?",
                (self.owner, time.time() + 45, time.time(), row["id"]),
            )
            return dict(row)

    def checkpoint(self, job):
        if self.closing.is_set():
            raise ExecutionError("execution_interrupted")
        with self.store.connection() as db:
            current = db.execute(
                "SELECT lease FROM execution_jobs "
                "WHERE id=? AND owner=? AND state='running' AND cancel=0",
                (job["id"], self.owner),
            ).fetchone()
            if not current:
                raise ExecutionError("execution_cancelled")
            self._pipeline_checkpoint(db, job)
            if current["lease"] < time.time() + 30:
                db.execute(
                    "UPDATE execution_jobs SET lease=?,updated=? WHERE id=? AND owner=?",
                    (time.time() + 45, time.time(), job["id"], self.owner),
                )

    def finish(self, job, *, error=None, log=""):
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE execution_jobs SET state=?,error=?,log=?,lease=0,updated=? "
                "WHERE id=? AND owner=? AND state='running'",
                ("failed" if error else "completed", error, log[-LOG_LIMIT:],
                 time.time(), job["id"], self.owner),
            ).rowcount
            if changed:
                self._pipeline_finished_job(db, job, error)
        if (
            changed and error and not job.get("preflight_failed")
            and job["action"] in ("setup", "test", "start")
            and error not in ("execution_cancelled", "execution_interrupted", "cleanup_pending",
                              "pipeline_account_changed", "pipeline_configuration_changed",
                              "pipeline_stopped")
            and job.get("context")
        ):
            try:
                manifests = manifest_context(self.directory(job["env_id"]) / "source")
                with self.store.connection() as db:
                    self.diagnoses.record(db, job, error, log, manifests)
            except Exception:
                logging.getLogger(__name__).warning("Execution diagnosis will retry from evidence.")

    def execute(self, job):
        env = self.get(job["env_id"])
        def check():
            self.checkpoint(job)
        action, log = job["action"], ""
        cleanup_required = env["state"] == "running" or env["error"] == "cleanup_pending"
        try:
            check()
            if action == "download":
                self.update(env["id"], state="downloading", error=None)
                with self.store.connection() as db:
                    repo = self.repository(db, env["repo_id"])
                revision, config = self.source.download(
                    repo, env["account_id"], env["account_version"],
                    self.directory(env["id"]) / "source", check,
                )
                check()
                config = self._preferred_configuration(env, config)
                reused = self._exact_environment(env, revision, check, config=config)
                self.update(env["id"], revision=revision, config=json.dumps(config),
                            reused_environment_id=reused,
                            preparation="same_version" if reused else "not_prepared",
                            state="reused" if reused else
                            "downloaded" if config["detected"] else "config_required")
            else:
                if action in ("setup", "test", "start"):
                    problem = desktop_guard(env["config"])
                    if problem:
                        raise ExecutionError(problem)
                    desktop = env["config"].get("project_profile", {}).get("kind")
                    if action == "start" and desktop == "python_desktop":
                        raise ExecutionError("desktop_service_unsupported")
                readiness = self.runtime.readiness()
                if not readiness["ready"]:
                    job["preflight_failed"] = True
                    raise ExecutionError(readiness["reason"])
                cleanup_required = True
                if action in ("stop", "delete"):
                    log = self.runtime.logs(env) if env["state"] == "running" else ""
                    if action == "delete":
                        self.runtime.cleanup(env)
                        directory = self.directory(env["id"])
                        if directory.exists():
                            shutil.rmtree(directory)
                    else:
                        self.runtime.remove(env)
                    self.update(env["id"], state="deleted" if action == "delete" else "stopped",
                                port=None, expires=None, error=None)
                elif action == "setup":
                    self.update(env["id"], state="installing", installed=0, test_result=None,
                                access_result=None, error=None)
                    self.runtime.cleanup(env)
                    env["image_id"] = self.runtime.image(env["config"]["language"], check)
                    self.update(env["id"], image_id=env["image_id"])
                    code, log = self._dependency_setup(env, check)
                    if code:
                        raise ExecutionError("setup_failed")
                    self.update(env["id"], installed=1, state="ready")
                elif action == "test":
                    self.update(env["id"], state="testing", error=None, test_result=None,
                                access_result=None)
                    code, log = self.runtime.run(env, action, check)
                    scope = offline_test_scope(env["config"], log)
                    no_executed_tests = scope is not None and not scope["executed"]
                    if not code and (no_executed_tests or
                                     job.get("pipeline_id") and empty_tests(log)):
                        code = 5
                    result = {
                        "exit_code": code, "revision": env["revision"],
                        "at": time.time(), "command": env["config"]["test"],
                    }
                    if scope is not None:
                        result["desktop_scope"] = scope
                    self.update(env["id"], test_result=json.dumps(result),
                                state="ready" if code == 0 else "test_failed")
                    if code:
                        raise ExecutionError("desktop_tests_not_executed" if no_executed_tests
                                             else "test_failed")
                elif action == "start":
                    self.update(env["id"], state="starting", error=None)
                    port = self.runtime.run(env, action, check)
                    if job.get("pipeline_id"):
                        with self.store.connection() as db:
                            self._pipeline_update(db, job["pipeline_id"], stage="access")
                    access = None
                    deadline = time.monotonic() + 120
                    while True:
                        check()
                        if not self.runtime.inspect(env)["Running"]:
                            log = self.runtime.logs(env)
                            raise ExecutionError("start_failed")
                        try:
                            if not self.runtime.listening(env):
                                raise OSError("Not listening yet")
                            with socket.create_connection(("127.0.0.2", port), timeout=1):
                                pass
                            # Release the bare connection before issuing HTTP; otherwise a
                            # single-threaded service waits for this idle client indefinitely.
                            if job.get("pipeline_id"):
                                access = self.probe_access(port)
                                if not access:
                                    raise OSError("HTTP not ready")
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                log = self.runtime.logs(env)
                                raise ExecutionError("start_not_listening")
                            time.sleep(0.3)
                    self.update(env["id"], state="running", port=port,
                                access_result=json.dumps(access) if access else None,
                                expires=time.time() + 1800)
                    log = self.runtime.logs(env)
                elif action == "access":
                    access = self.probe_access(env["port"])
                    if not access or not self.runtime.inspect(env)["Running"]:
                        raise ExecutionError("start_not_listening")
                    self.update(env["id"], access_result=json.dumps(access), error=None)
                    log = "RepoWatch: 已确认项目 HTTP 地址可访问。"
            check()
            self.finish(job, log=log)
        except Exception as exc:
            if isinstance(exc, ExecutionError) and exc.log:
                log = exc.log
            code = exc.code if isinstance(exc, GitHubError) else str(exc) if isinstance(
                exc, (ExecutionError, Conflict, Missing)
            ) else "execution_failed"
            # Do not persist arbitrary exception text that may contain remote URLs or credentials.
            if cleanup_required:
                try:
                    self.runtime.remove(env)
                except Exception:
                    code = "cleanup_pending"
            state = {"setup": "setup_failed", "test": "test_failed"}.get(action, "failed")
            if code in ("execution_cancelled", "execution_interrupted"):
                state = "stopped"
            self.update(env["id"], state=state, error=code, port=None, expires=None)
            self.finish(job, error=code, log=log)

    def reconcile(self):
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            stale = [dict(r) for r in db.execute(
                "SELECT * FROM execution_jobs WHERE state='running' AND lease<?", (now,)
            )]
            for job in stale:
                # Claim recovery before cleanup so another worker cannot replay it.
                db.execute("UPDATE execution_jobs SET owner=?,lease=? WHERE id=?",
                           (self.owner, now + 45, job["id"]))
        for job in stale:
            env = self.get(job["env_id"])
            try:
                if job["action"] != "download":
                    self.runtime.remove(env)
            except ExecutionError:
                continue
            self.update(env["id"], state="stopped", error="execution_interrupted",
                        port=None, expires=None, installed=0 if job["action"] == "setup"
                        else int(env["installed"]))
            self.finish(job, error="execution_interrupted")
        with self.store.connection() as db:
            live = [r["id"] for r in db.execute(
                "SELECT id FROM environments WHERE state='running' OR error='cleanup_pending'"
            )]
        for env_id in live:
            env = self.get(env_id)
            if env["busy"]:
                continue
            try:
                expired = env["expires"] and env["expires"] <= now
                if env["error"] == "cleanup_pending" or expired:
                    self.runtime.remove(env)
                    self.update(env_id, state="stopped", port=None, expires=None,
                                error=None if expired else "execution_interrupted")
                elif not self.runtime.inspect(env)["Running"]:
                    self.runtime.remove(env)
                    self.update(env_id, state="stopped", port=None, expires=None)
            except ExecutionError:
                # No reachable daemon means the old URL is no longer a verified live environment.
                self.update(env_id, state="failed", port=None, expires=None,
                            error="cleanup_pending")

    def tick(self):
        self.reconcile()
        self.reconcile_pipelines()
        job = self.claim()
        if job:
            self.execute(job)
        return bool(job)

    async def run(self):
        while not self.closing.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                # Recover leased jobs on the next tick without logging secret-bearing exceptions.
                logging.getLogger(__name__).warning("Execution worker will retry after an error.")
            await asyncio.sleep(1)
        await asyncio.to_thread(self.stop_previews)

    def stop_previews(self):
        with self.store.connection() as db:
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM environments WHERE state='running'"
            )]
        for env_id in ids:
            env = self.get(env_id)
            try:
                self.runtime.remove(env)
                self.update(env_id, state="stopped", port=None, expires=None)
            except ExecutionError:
                self.update(env_id, state="failed", port=None, expires=None,
                            error="cleanup_pending")
