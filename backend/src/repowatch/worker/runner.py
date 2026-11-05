import json
import shlex
import time
from hashlib import sha256

from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.application.workspaces import command_identity
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json

COPY_AND_RUN = "mkdir -p /work/source /deps /out; cp -a /source/. /work/source/; cd /work/source; "


def preparation_report_from_log(content):
    marker = b"REPOWATCH_ENVIRONMENT_UNAVAILABLE: "
    report_line = next((line for line in content.splitlines() if line.startswith(marker)), None)
    if report_line is None:
        return None
    try:
        return json.loads(report_line[len(marker) :])
    except json.JSONDecodeError:
        return {"reason": "dependency_install_failed", "details": "invalid_report"}


class Runner:
    def __init__(self, services, workspaces, *, clock=time.time):
        self.services, self.workspaces, self.clock = services, workspaces, clock
        self.backend = workspaces.backend

    def check(self, token, workspace_id, argv, *, timeout=60, operation_key, preparation=None):
        workspace = self.workspaces.require(token, workspace_id, now=self.clock())
        preparation = preparation or {"steps": [], "gaps": [], "declarations": []}
        recollect = None
        with self.services.store.read() as records:
            old = records.one(
                "budget_reservations",
                owner_task_id=required(records, "runs", id=token.run_id)["budget_owner_task_id"],
                operation_key=operation_key,
            )
            if old:
                results = records.find("runner_results", run_id=old["run_id"])
                for row in results:
                    command = required(records, "command_states", id=row["command_id"])
                    if json.loads(command["identity_json"]).get("reservation_id") == old["id"]:
                        result = json.loads(row["result_json"])
                        if (
                            result["argv"] != list(argv)
                            or result["workspace_id"] != workspace_id
                            or result["sha"] != workspace["sha"]
                            or result["image"] != workspace["image"]
                            or result["manifest_sha256"] != workspace["manifest_sha256"]
                            or result.get("timeout") != timeout
                            or result.get("preparation") != preparation
                        ):
                            raise PermissionError("同一命令操作参数已变化")
                        try:
                            self.services.artifacts.require(
                                result["log_artifact_id"], now=self.clock(), block_on_failure=False
                            )
                        except PermissionError:
                            if command["state"] not in {"stopped", "isolated"}:
                                raise PermissionError("日志缺失且旧命令未核实停止") from None
                            recollect = result["log_artifact_id"]
                        else:
                            return result
        if recollect:
            return self.check(
                token,
                workspace_id,
                argv,
                timeout=timeout,
                operation_key=operation_key + ":recollect:" + recollect,
                preparation=preparation,
            )
        identity = command_identity(workspace, argv, timeout)
        identity["generation"] = token.generation
        identifier = identity["container"].removeprefix("repowatch-command-")
        executor = BudgetedExecutor(self.services.budgets, self.services.queue, clock=self.clock)
        prepare = ""
        if preparation["gaps"]:
            report = canonical_json(
                {
                    "reason": "dependency_preparation_unavailable",
                    "gaps": preparation["gaps"],
                    "steps": preparation["steps"],
                }
            )
            prepare = (
                "printf '%s\\n' "
                + shlex.quote("REPOWATCH_ENVIRONMENT_UNAVAILABLE: " + report)
                + "; exit 125; "
            )
        else:
            for step in preparation["steps"]:
                report = canonical_json({"reason": "dependency_install_failed", "step": step})
                prepare += (
                    shlex.join(step)
                    + " || { printf '%s\\n' "
                    + shlex.quote("REPOWATCH_ENVIRONMENT_UNAVAILABLE: " + report)
                    + "; exit 125; }; "
                )
        verify_declarations = ""
        for declaration in preparation["declarations"]:
            verify_declarations += (
                'test "$(sha256sum '
                + shlex.quote(declaration["path"])
                + " | cut -d ' ' -f 1)\" = "
                + shlex.quote(declaration["sha256"])
                + " || { printf 'REPOWATCH_ENVIRONMENT_UNAVAILABLE: declaration changed'; "
                + "exit 125; }; "
            )
        command_script = (
            COPY_AND_RUN
            + verify_declarations
            + prepare
            + verify_declarations
            + "export PYTHONPATH=/deps/python; "
            + 'export PATH="/work/source/.venv/bin:$PATH"; exec "$@"'
        )

        def execute():
            with self.services.store.write() as records:
                execution_record(records, token, self.clock())
                reservation = required(
                    records, "budget_reservations", run_id=token.run_id, operation_key=operation_key
                )
                identity["reservation_id"] = reservation["id"]
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
                business_event(records, "runner_intent", command_id=identifier, run_id=token.run_id)
            name = identity["container"]
            self.backend.docker(
                "create",
                "--name",
                name,
                "--label",
                "repowatch.command=" + name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--mount",
                "type=volume,src=" + workspace["volume"] + ",dst=/source,readonly",
                "--tmpfs",
                "/work:rw,exec,size=536870912",
                "--tmpfs",
                "/tmp:rw,size=134217728",
                "--tmpfs",
                "/deps:rw,exec,size=268435456",
                "--tmpfs",
                "/out:rw,size=134217728",
                "--env",
                "HOME=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PIP_CACHE_DIR=/deps/pip",
                "--env",
                "npm_config_cache=/deps/npm",
                "--entrypoint",
                "sh",
                workspace["image"],
                "-eu",
                "-c",
                command_script,
                "runner",
                *argv,
            )
            with self.services.store.write() as records:
                execution_record(records, token, self.clock())
                records.change("command_states", {"id": identifier}, {"state": "running"})
            self.services.queue.guard(
                token, now=self.clock(), purpose="tool", reservation_id=identity["reservation_id"]
            )
            started = self.clock()
            self.backend.docker("start", name)
            timed_out = False
            control_stopped = False
            while True:
                proof = self.backend.command_status(identity)
                if proof["stopped"]:
                    break
                try:
                    self.services.queue.guard(
                        token,
                        now=self.clock(),
                        purpose="tool",
                        reservation_id=identity["reservation_id"],
                    )
                except PermissionError:
                    control_stopped = True
                    proof = self.backend.command_status(identity, stop=True)
                    break
                if self.clock() - started >= timeout:
                    timed_out = True
                    proof = self.backend.command_status(identity, stop=True)
                    break
                time.sleep(0.02)
            if not proof["stopped"] or not proof["identity_verified"]:
                raise PermissionError("命令未核实停止")
            log = self.backend.docker("logs", name, check=False)
            content = log.stdout + b"\n" + log.stderr
            truncated = log.returncode != 0 or len(content) > 8 * 1024 * 1024
            content = content[: 8 * 1024 * 1024]
            preparation_report = preparation_report_from_log(content)
            artifact = self.services.artifacts.register(
                token,
                "runner-log:" + identifier,
                content,
                context={
                    "producer": "docker_runner",
                    "workspace_id": workspace_id,
                    "command_id": identifier,
                    "sha": workspace["sha"],
                    "argv": list(argv),
                },
                now=self.clock(),
            )
            result = {
                "command_id": identifier,
                "workspace_id": workspace_id,
                "argv": list(argv),
                "sha": workspace["sha"],
                "manifest_sha256": workspace["manifest_sha256"],
                "image": workspace["image"],
                "exit_code": proof.get("exit_code"),
                "timed_out": timed_out,
                "control_stopped": control_stopped,
                "timeout": timeout,
                "preparation": preparation,
                "preparation_report": preparation_report,
                "oom_killed": proof.get("oom_killed", False),
                "log_artifact_id": artifact,
                "log_sha256": sha256(content).hexdigest(),
                "log_truncated": truncated,
                "elapsed_ms": round((self.clock() - started) * 1000),
            }
            with self.services.store.write() as records:
                execution_record(records, token, self.clock())
                records.change(
                    "command_states",
                    {"id": identifier},
                    {"state": "stopped", "evidence_json": canonical_json(proof)},
                )
                records.add(
                    "runner_results",
                    {
                        "command_id": identifier,
                        "run_id": token.run_id,
                        "workspace_id": workspace_id,
                        "result_json": canonical_json(result),
                        "artifact_id": artifact,
                        "created_at": self.clock(),
                    },
                )
            self.backend.docker("rm", name)
            return result

        return executor.execute(
            token,
            operation_key,
            BudgetAmounts(active_ms=round(timeout * 1000)),
            execute,
            lambda _: {},
            purpose="tool",
        )
