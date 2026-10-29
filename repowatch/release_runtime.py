"""Pinned startup, identity-checked processes and a model-independent supervisor."""

import hmac
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from . import recovery
from .release_manifest import CONFIG_KEYS, Manifest, asset_files
from .release_state import Journal, atomic_json, existing_database, idle, locked, read_json
from .store import Conflict


def port_free(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def local_authorization(manifest):
    with existing_database(manifest.database) as db:
        row = db.execute("SELECT binding FROM release_authorizations WHERE "
                         "kind='deploy' AND target=? AND revoked=0", (manifest.id,)).fetchone()
        import json

        if not row or json.loads(row[0]) != manifest.binding:
            raise Conflict("release_deploy_authorization_required")
        account = db.execute("SELECT a.version,a.state,c.active_account_id FROM github_accounts a "
                             "JOIN github_configuration c ON c.id=1 WHERE a.id=?",
                             (manifest.binding["account_id"],)).fetchone()
        if (not account or account["state"] != "verified"
                or account["version"] != manifest.binding["account_version"]
                or account["active_account_id"] != manifest.binding["account_id"]):
            raise Conflict("release_account_changed")


def process_identity(pid):
    process = psutil.Process(pid)
    return {"pid": pid, "created": process.create_time(), "exe": process.exe(),
            "command": process.cmdline()}


def matching_process(record):
    if not record or not record.get("pid"):
        return None
    try:
        process = psutil.Process(record["pid"])
        if (process.create_time() == record["created"]
                and process.exe() == record["exe"]
                and process.cmdline() == record["command"]):
            return process
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, KeyError):
        raise Conflict("release_process_identity_unknown") from None
    raise Conflict("release_process_identity_changed")


class NativeRuntime:
    def __init__(self, manifest, path):
        self.manifest, self.path = manifest, Path(path)
        self.root = Path(manifest.state_root)

    def record(self):
        value = read_json(self.root / "process.json")
        if value and value.get("release_id") != self.manifest.id:
            raise Conflict("release_process_manifest_mismatch")
        return value

    def health(self):
        record = self.record()
        if not record or not record.get("pid") or not matching_process(record):
            raise Conflict("release_process_not_running")
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            response = client.get(f"http://127.0.0.1:{self.manifest.port}/api/release-health",
                                  headers={"X-RepoWatch-Health": record["token"]})
            if response.status_code != 200:
                raise Conflict("release_not_ready")
            actual = response.json()
            expected = {
                "id": self.manifest.id, "commit": self.manifest.commit,
                "code_root": self.manifest.code_root, "python": self.manifest.python,
                "frontend": recovery.digest(self.manifest.frontend),
                "database": self.manifest.database, "database_id": self.manifest.database_id,
                "config_digest": self.manifest.config_digest, "pid": record["pid"], "ready": True,
            }
            mismatches = {key: {"expected": value, "actual": actual.get(key)}
                          for key, value in expected.items() if actual.get(key) != value}
            if mismatches:
                error = Conflict("release_health_mismatch")
                error.mismatches = mismatches
                raise error
            values = self.manifest.validate_files()
            if actual.get("monitoring") != (values.get("REPOWATCH_LANGFUSE_ENABLED") == "1"):
                raise Conflict("release_monitoring_not_initialized")
            # Verify bytes actually served by HTTP, not only the on-disk build label.
            import hashlib

            for name, digest in self.manifest.frontend.items():
                uri = "/" if name == "index.html" else "/" + name
                asset = client.get(f"http://127.0.0.1:{self.manifest.port}{uri}")
                if asset.status_code != 200 or hashlib.sha256(asset.content).hexdigest() != digest:
                    error = Conflict("release_served_frontend_mismatch")
                    error.mismatches = {"asset": name, "expected": digest,
                                        "actual": hashlib.sha256(asset.content).hexdigest()}
                    raise error
        return actual

    def start(self):
        self.manifest.validate_files()
        local_authorization(self.manifest)
        record = self.record()
        if record and record.get("pid") and matching_process(record):
            raise Conflict("release_process_already_running")
        if record and record.get("state") == "starting":
            raise Conflict("release_start_receipt_unknown")
        if not port_free(self.manifest.port):
            raise Conflict("release_port_conflict")
        token = secrets.token_hex(32)
        atomic_json(self.root / "process.json",
                    {"release_id": self.manifest.id, "token": token, "state": "starting"})
        environment = {key: value for key, value in os.environ.items()
                       if key not in CONFIG_KEYS and not key.startswith(("PYTHON", "REPOWATCH_"))}
        environment["REPOWATCH_RELEASE_TOKEN"] = token
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / (self.manifest.id + ".log")).open("ab") as output:
            process = psutil.Popen(
                self.manifest.command(self.path), cwd=self.manifest.code_root, env=environment,
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return process

    def wait(self, seconds=45):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                return self.health()
            except (Conflict, httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(0.25)
        if isinstance(last, Conflict):
            raise last
        raise Conflict("release_startup_failed")

    def stop(self):
        record = self.record()
        if record and record.get("state") == "starting":
            self.clear_absent_start()
            return
        process = matching_process(record)
        if process:
            # psutil rechecks PID + birth time when signalling. Never kill by port or PID alone.
            process.terminate()
            try:
                process.wait(timeout=15)
            except psutil.TimeoutExpired:
                process = matching_process(record)
                if process:
                    process.kill()
                    process.wait(timeout=5)
        (self.root / "process.json").unlink(missing_ok=True)

    def clear_absent_start(self):
        record = self.record()
        if not record or record.get("state") != "starting" or not port_free(self.manifest.port):
            raise Conflict("release_start_receipt_unknown")
        expected = self.manifest.command(self.path)[1:]
        interpreter_name = Path(self.manifest.python).name.lower()
        for process in psutil.process_iter(["name"]):
            name = (process.info["name"] or "").lower()
            if name != interpreter_name and "python" not in name:
                continue
            try:
                if process.cmdline()[1:] == expected and hmac.compare_digest(
                        process.environ().get("REPOWATCH_RELEASE_TOKEN", ""), record["token"]):
                    raise Conflict("release_start_still_running")
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                raise Conflict("release_process_identity_unknown") from None
        (self.root / "process.json").unlink()


def serve(path):
    manifest = Manifest.load(path)
    if Path(sys.executable).resolve() != Path(manifest.python):
        raise Conflict("release_interpreter_mismatch")
    if Path(__file__).resolve().parent.parent != Path(manifest.code_root):
        raise Conflict("release_loaded_code_mismatch")
    values = manifest.validate_files()
    local_authorization(manifest)
    for key in CONFIG_KEYS:
        os.environ.pop(key, None)
    os.environ.update(values)
    os.environ["REPOWATCH_DB"] = manifest.database
    os.environ["REPOWATCH_RELEASE_MANIFEST"] = str(path)
    token = os.getenv("REPOWATCH_RELEASE_TOKEN", "")
    initial = read_json(Path(manifest.state_root) / "process.json")
    if (not token or not initial or initial.get("release_id") != manifest.id
            or not hmac.compare_digest(initial.get("token", ""), token)):
        raise Conflict("release_start_intent_required")
    record = {**process_identity(os.getpid()), "release_id": manifest.id, "token": token,
              "state": "running"}
    atomic_json(Path(manifest.state_root) / "process.json", record)
    import uvicorn

    from .app import create_app

    app = create_app(Path(manifest.database), start_worker=manifest.workers)
    uvicorn.run(app, host="127.0.0.1", port=manifest.port, access_log=False, log_level="warning")


def runtime_health(store, telemetry, manifest, ready):
    manifest.validate_files()
    if (Path(store.path).resolve() != Path(manifest.database)
            or Path(__file__).resolve().parent.parent != Path(manifest.code_root)):
        raise Conflict("release_loaded_binding_mismatch")
    with store.connection() as db:
        identity = db.execute(
            "SELECT value FROM settings WHERE key='release_database_id'").fetchone()
    return {
        "id": manifest.id, "commit": manifest.commit, "code_root": manifest.code_root,
        "python": str(Path(sys.executable).resolve()),
        "frontend": recovery.digest(asset_files(Path(manifest.code_root))),
        "database": str(Path(store.path).resolve()), "database_id": identity[0],
        "config_digest": manifest.config_digest, "pid": os.getpid(),
        "ready": ready, "monitoring": telemetry.status()["enabled"],
    }


class Supervisor:
    def __init__(self, root, *, runtime_factory=NativeRuntime):
        self.root, self.runtime_factory = Path(root), runtime_factory
        self.journal = Journal(root)

    def status(self, manifest, state, reason=None, **details):
        path = self.root / "status.json"
        old = read_json(path, {})
        new = {"release_id": manifest.id, "state": state, "reason": reason, **details}
        if old != new:
            atomic_json(path, new)
            if state == "blocked":
                self.journal.event(manifest.id, "blocked", reason,
                                   commit=manifest.commit, **details)

    def tick(self):
        with locked(self.root / "action.lock"):
            selected = read_json(self.root / "selection.json")
            if not selected:
                return
            manifest = Manifest.load(selected["current"])
            if Path(manifest.state_root) != self.root:
                raise Conflict("release_state_root_mismatch")
            runtime = self.runtime_factory(manifest, selected["current"])
            if read_json(self.root / "pending.json"):
                self.status(manifest, "blocked", "release_interrupted_requires_reconciliation")
                return
            try:
                runtime.health()
                if read_json(self.root / "restart.json"):
                    with existing_database(manifest.database) as db:
                        db.execute("UPDATE release_control SET draining=NULL "
                                   "WHERE id=1 AND draining=?", (manifest.id,))
                    (self.root / "restart.json").unlink()
                self.status(manifest, "healthy")
                return
            except (httpx.HTTPError, ValueError, Conflict) as exc:
                reason = str(exc) if isinstance(exc, Conflict) else "release_not_ready"
            try:
                local_authorization(manifest)
                record = runtime.record()
                process = matching_process(record)
                if not process and not port_free(manifest.port):
                    raise Conflict("release_port_conflict")
                if record and record.get("state") == "starting":
                    raise Conflict("release_start_receipt_unknown")
                with existing_database(manifest.database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    drain = db.execute(
                        "SELECT draining FROM release_control WHERE id=1").fetchone()[0]
                    restart = read_json(self.root / "restart.json")
                    if drain and (drain != manifest.id or not restart
                                  or restart.get("release_id") != manifest.id):
                        self.status(manifest, "waiting", "release_draining")
                        return
                    if process and not idle(db):
                        self.status(manifest, "waiting", "release_active_tasks")
                        return
                    atomic_json(self.root / "restart.json",
                                {"release_id": manifest.id, "manifest": selected["current"]})
                    db.execute("UPDATE release_control SET draining=? WHERE id=1", (manifest.id,))
                self.journal.reserve_restart()
                if process:
                    runtime.stop()
                else:
                    (self.root / "process.json").unlink(missing_ok=True)
                runtime.start()
                runtime.wait()
                self.journal.event(manifest.id, "restart", reason, commit=manifest.commit)
                with existing_database(manifest.database) as db:
                    db.execute("UPDATE release_control SET draining=NULL "
                               "WHERE id=1 AND draining=?", (manifest.id,))
                (self.root / "restart.json").unlink()
                self.status(manifest, "healthy")
            except (Conflict, OSError, httpx.HTTPError, ValueError) as exc:
                self.status(manifest, "blocked",
                            str(exc) if isinstance(exc, Conflict) else "release_supervision_failed")

    def run(self):
        with locked(self.root / "supervisor.lock"):
            while True:
                try:
                    self.tick()
                except Conflict as exc:
                    if str(exc) == "release_action_busy":
                        time.sleep(5)
                        continue
                    state = {"state": "blocked", "reason": str(exc)}
                    if read_json(self.root / "status.json", {}) != state:
                        atomic_json(self.root / "status.json", state)
                        self.journal.event("supervisor", "blocked", str(exc))
                except Exception:
                    # No raw exception text: credentials/config bodies are never journalled.
                    state = {"state": "blocked", "reason": "release_supervision_failed"}
                    try:
                        if read_json(self.root / "status.json", {}) != state:
                            atomic_json(self.root / "status.json", state)
                            self.journal.event("supervisor", "blocked",
                                               "release_supervision_failed")
                    except Exception:
                        pass
                time.sleep(5)
