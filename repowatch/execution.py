"""Durable repository environments. Execution results never authorize a finding."""

import asyncio
import json
import logging
import re
import shutil
import socket
import threading
import time

from .diagnosis import CONFIG_KEYS, Diagnoses, Patch, fingerprint, manifest_context
from .docker_runtime import LOG_LIMIT, DockerRuntime
from .github_accounts import GitHubError
from .repository_source import IMAGES, ExecutionError, RepositorySource
from .store import Conflict, Missing, uid


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS environments (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
            account_id INTEGER NOT NULL, account_version INTEGER NOT NULL,
            revision TEXT, config TEXT, image_id TEXT,
            state TEXT NOT NULL DEFAULT 'queued', installed INTEGER NOT NULL DEFAULT 0,
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
    columns = {row["name"] for row in db.execute("PRAGMA table_info(execution_jobs)")}
    if "context" not in columns:
        db.execute("ALTER TABLE execution_jobs ADD COLUMN context TEXT")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('execution_namespace', ?)", (uid("local"),))


class Execution:
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
        for key in ("config", "test_result"):
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
    def enqueue(db, env_id, action):
        now = time.time()
        env = Execution.row(db, env_id)
        context = json.dumps({
            "revision": env["revision"], "config": json.loads(env["config"] or "{}"),
        })
        db.execute(
            "INSERT INTO execution_jobs(id,env_id,action,created,updated,context) "
            "VALUES(?,?,?,?,?,?)",
            (uid("exec"), env_id, action, now, now, context),
        )

    def create(self, repo_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository(db, repo_id)
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
        config = {key: config[key] for key in CONFIG_KEYS}
        if (
            config["language"] not in IMAGES or type(config["port"]) is not int
            or not 1024 <= config["port"] <= 65535 or type(config["install_network"]) is not bool
            or any(not isinstance(config[k], str) or len(config[k]) > 4000
                   or "\x00" in config[k] for k in ("install", "test", "start"))
        ):
            raise Conflict("invalid_execution_config")
        return config

    def configure(self, env_id, version, config):
        config = self.validate_config(config)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            env = self.editable(db, env_id, version)
            if not env["revision"] or env["state"] in ("running", "deleted"):
                raise Conflict("environment_not_configurable")
            db.execute(
                "UPDATE environments SET config=?,state='downloaded',installed=0,test_result=NULL,"
                "image_id=NULL,error=NULL,version=version+1,updated=? WHERE id=?",
                (json.dumps({**config, "detected": True, "reason": "owner_override"}),
                 time.time(), env_id),
            )
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
            config = self.validate_config({**config, **changes})
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
            if action == "stop" and pending:
                db.execute("UPDATE execution_jobs SET cancel=1 WHERE id=?", (pending["id"],))
                return {"accepted": True}
            self.editable(db, env_id, version)
            if env["state"] == "deleted":
                raise Conflict("environment_deleted")
            if action in ("setup", "test", "start"):
                if not env["revision"] or env["state"] == "running":
                    raise Conflict("environment_not_ready")
                config = json.loads(env["config"])
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
        if (
            changed and error and job["action"] in ("setup", "test", "start")
            and error not in ("execution_cancelled", "execution_interrupted", "cleanup_pending")
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
                self.update(env["id"], revision=revision, config=json.dumps(config),
                            state="downloaded" if config["detected"] else "config_required")
            else:
                readiness = self.runtime.readiness()
                if not readiness["ready"]:
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
                                error=None)
                    # Every setup starts from retained source, removing untrusted mutable data.
                    self.runtime.cleanup(env)
                    env["image_id"] = self.runtime.image(env["config"]["language"], check)
                    self.update(env["id"], image_id=env["image_id"])
                    self.runtime.seed(env, self.directory(env["id"]) / "source", check)
                    code, log = self.runtime.run(env, action, check)
                    if code:
                        raise ExecutionError("setup_failed")
                    self.update(env["id"], installed=1, state="ready")
                elif action == "test":
                    self.update(env["id"], state="testing", error=None, test_result=None)
                    code, log = self.runtime.run(env, action, check)
                    self.update(env["id"], test_result=json.dumps({
                        "exit_code": code, "revision": env["revision"],
                        "at": time.time(), "command": env["config"]["test"],
                    }), state="ready" if code == 0 else "test_failed")
                    if code:
                        raise ExecutionError("test_failed")
                elif action == "start":
                    self.update(env["id"], state="starting", error=None)
                    port = self.runtime.run(env, action, check)
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
                                break
                        except OSError:
                            if time.monotonic() > deadline:
                                log = self.runtime.logs(env)
                                raise ExecutionError("start_not_listening")
                            time.sleep(0.3)
                    self.update(env["id"], state="running", port=port,
                                expires=time.time() + 1800)
                    log = self.runtime.logs(env)
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
