"""先登记意图、再加入作业、持久身份后才打开启动门。"""

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from repowatch.adapters.windows_jobs import WindowsJob, process_identity
from repowatch.application.storage import BusinessStore, ConflictError
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.task_records import ExecutionToken, canonical_json, content_digest

LAUNCHER = """
import json, subprocess, sys
line = sys.stdin.buffer.readline()
if not line:
    sys.exit(125)
plan = json.loads(line)
child = subprocess.Popen(plan['argv'], cwd=plan['cwd'], env=plan['env'], stdin=subprocess.DEVNULL)
sys.exit(child.wait())
"""


class CommandSupervisor:
    def __init__(self, store: BusinessStore, staging_dir: Path, orchestrator, *, clock=time.time):
        self.store, self.orchestrator, self.clock = store, orchestrator, clock
        self.staging_dir = staging_dir.resolve()
        self._handles = {}
        self._lock = threading.Lock()

    def start(
        self,
        token: ExecutionToken,
        reservation_id: str,
        argv: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
    ) -> str:
        if (
            not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not Path(argv[0]).is_absolute()
            or not Path(argv[0]).is_file()
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("需要明确可执行文件、参数及有限超时")
        self.orchestrator.guard(
            token, now=self.clock(), purpose="tool", reservation_id=reservation_id
        )
        with self.store.read() as records:
            reservation = required(records, "budget_reservations", id=reservation_id)
            if timeout * 1000 > json.loads(reservation["upper_json"])["active_ms"]:
                raise PermissionError("命令超时不能超过已预留上界")
        identifier = str(uuid4())
        identity = {
            "job_name": f"Local\\RepoWatch-{identifier}",
            "pid": None,
            "created": None,
            "argv_sha256": content_digest(argv),
            "deadline": self.clock() + timeout,
            "reservation_id": reservation_id,
        }
        with self.store.write() as records:
            execution_record(records, token, self.clock())
            records.add(
                "command_states",
                {
                    "id": identifier,
                    "run_id": token.run_id,
                    "generation": token.generation,
                    "state": "intent",
                    "identity_json": canonical_json(identity),
                },
            )
            business_event(records, "command_intent", command_id=identifier, run_id=token.run_id)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        output = (self.staging_dir / f"{identifier}.log").open("wb")
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment["PATH"] = os.defpath
        job, process = None, None
        try:
            job = WindowsJob(identity["job_name"], create=True)
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", LAUNCHER],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            job.assign(process)
            identity.update(pid=process.pid, created=process_identity(process.pid))
            with self.store.write() as records:
                execution_record(records, token, self.clock())
                records.change(
                    "command_states",
                    {"id": identifier, "state": "intent"},
                    {"identity_json": canonical_json(identity), "state": "running"},
                )
                business_event(records, "command_bound", command_id=identifier, run_id=token.run_id)
            with self._lock:
                self._handles[identifier] = (job, process, output)
            # 任一步失败都不会把用户命令交给启动器；stdin EOF 使未绑定的启动器退出。
            process.stdin.write(
                (
                    canonical_json(
                        {"argv": argv, "cwd": str(cwd) if cwd else None, "env": environment}
                    )
                    + "\n"
                ).encode()
            )
            process.stdin.close()
            return identifier
        except BaseException:
            if process is not None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if job is not None:
                    job.stop()
                process.wait(timeout=5)
            if job is not None:
                job.close()
            output.close()
            raise

    def inspect(self, identifier: str) -> dict:
        with self.store.read() as records:
            row = dict(required(records, "command_states", id=identifier))
        identity = json.loads(row["identity_json"])
        if row["state"] in {"stopped", "isolated"}:
            return json.loads(row["evidence_json"])
        if identity.get("kind") == "docker":
            from repowatch.adapters.docker_workspaces import DockerWorkspaces

            backend = DockerWorkspaces(self.staging_dir / "docker", None)
            proof = backend.command_status(identity)
            if proof["stopped"]:
                with self.store.write() as records:
                    records.change(
                        "command_states",
                        {"id": identifier},
                        {"state": "stopped", "evidence_json": canonical_json(proof)},
                    )
            return proof
        if identity.get("job_name") != f"Local\\RepoWatch-{identifier}":
            return {
                "stopped": False,
                "identity_verified": False,
                "active_processes": None,
                "reason": "legacy_identity_requires_reconciliation",
            }
        try:
            job = WindowsJob(identity["job_name"], create=False)
        except FileNotFoundError:
            current = process_identity(identity["pid"]) if identity["pid"] is not None else None
            stopped = current is None or current != identity["created"]
            proof = {
                "stopped": stopped,
                "active_processes": 0 if stopped else None,
                "identity_verified": True,
                "reason": "job_absent_and_identity_checked",
            }
        else:
            try:
                count = job.active()
                proof = {
                    "stopped": count == 0,
                    "active_processes": count,
                    "identity_verified": True,
                    "reason": "job_process_count",
                }
            finally:
                job.close()
        if proof["stopped"]:
            with self.store.write() as records:
                current = required(records, "command_states", id=identifier)
                if current["identity_json"] != row["identity_json"]:
                    raise ConflictError("核验期间命令身份变化")
                records.change(
                    "command_states",
                    {"id": identifier},
                    {"state": "stopped", "evidence_json": canonical_json(proof)},
                )
                business_event(
                    records, "command_stopped", command_id=identifier, run_id=row["run_id"]
                )
            self._close_handles(identifier)
        return proof

    def stop(self, identifier: str) -> dict:
        with self.store.read() as records:
            row = required(records, "command_states", id=identifier)
            identity = json.loads(row["identity_json"])
        if identity.get("kind") == "docker":
            from repowatch.adapters.docker_workspaces import DockerWorkspaces

            DockerWorkspaces(self.staging_dir / "docker", None).command_status(identity, stop=True)
            return self.inspect(identifier)
        if row["state"] not in {"stopped", "isolated"}:
            try:
                job = WindowsJob(identity["job_name"], create=False)
            except FileNotFoundError:
                pass
            else:
                try:
                    job.stop()
                finally:
                    job.close()
        return self.inspect(identifier)

    def wait(self, identifier: str) -> dict:
        with self.store.read() as records:
            identity = json.loads(
                required(records, "command_states", id=identifier)["identity_json"]
            )
        while True:
            proof = self.inspect(identifier)
            if proof["stopped"]:
                return proof
            if self.clock() >= identity["deadline"]:
                proof = self.stop(identifier)
                proof = {**proof, "timed_out": True}
                with self.store.write() as records:
                    records.change(
                        "command_states",
                        {"id": identifier},
                        {"evidence_json": canonical_json(proof)},
                    )
                return proof
            time.sleep(0.01)

    def _close_handles(self, identifier):
        with self._lock:
            handles = self._handles.pop(identifier, None)
        if handles:
            job, process, output = handles
            process.wait(timeout=5)
            job.close()
            output.close()

    def close(self):
        with self._lock:
            identifiers = list(self._handles)
        for identifier in identifiers:
            self.stop(identifier)
