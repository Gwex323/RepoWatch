"""异常运行只读核对与明确放弃旧执行；不重发模型、命令或远端写入。"""

import json
import sqlite3

from repowatch.adapters.storage.artifact_files import verify_file
from repowatch.adapters.windows_jobs import process_identity
from repowatch.application.budget_accounting import ledger_values, save_ledger
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.safety import assert_quiescent
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, bump_task, business_event, required
from repowatch.domain.budget import DIMENSIONS, plus, vector
from repowatch.domain.task_records import canonical_json, content_digest, require_time, stable_id


def _database_basis(records, run_id):
    run = dict(required(records, "runs", id=run_id))
    task = dict(required(records, "tasks", id=run["task_id"]))
    authority = run
    seen = {run_id}
    while authority["parent_run_id"] is not None:
        authority = required(records, "runs", id=authority["parent_run_id"])
        if authority["id"] in seen:
            raise PermissionError("运行委派链存在循环")
        seen.add(authority["id"])
    family = [run]
    for item in family:
        family.extend(
            dict(child)
            for child in records.find("runs", parent_run_id=item["id"])
            if child["id"] not in {row["id"] for row in family}
        )

    def related(collection):
        return [
            dict(row) for member in family for row in records.find(collection, run_id=member["id"])
        ]

    return {
        "run": run,
        "family": family,
        "authority_run_id": authority["id"],
        "task": task,
        "repository": (
            dict(required(records, "repositories", id=task["repository_id"]))
            if task["repository_id"] is not None
            else None
        ),
        "lease": dict(required(records, "execution_lease", singleton=1)),
        "commands": related("command_states"),
        "reservations": related("budget_reservations"),
        "operations": [dict(row) for row in records.find("publish_operations", task_id=task["id"])],
        "stages": related("stage_intents"),
        "artifact_intents": related("artifact_intents"),
        "provider_invocations": related("provider_invocations"),
        "runtime_stages": related("runtime_stages"),
    }


class RecoveryService:
    def __init__(self, store, settings, *, commands=None):
        self.store, self.settings, self.commands = store, settings, commands

    def report(self, run_id: str, *, now: float) -> dict:
        require_time(now)
        if self.commands:
            with self.store.read() as records:
                commands = _database_basis(records, run_id)["commands"]
            for command in commands:
                if command["state"] not in {"stopped", "isolated"}:
                    self.commands.inspect(command["id"])
        with self.store.read() as records:
            database = _database_basis(records, run_id)
            artifact_ids = {
                identifier
                for stage in database["stages"]
                for identifier in json.loads(stage["result_json"] or "{}").get("artifact_ids", [])
            }
            artifact_ids.update(
                row["id"]
                for member in database["family"]
                for row in records.find("artifacts", run_id=member["id"])
            )
            artifacts = [
                dict(required(records, "artifacts", id=identifier))
                for identifier in sorted(artifact_ids)
            ]
        run, task, repository, lease = (
            database[key] for key in ("run", "task", "repository", "lease")
        )
        snapshot = json.loads(run["snapshot_json"])
        worker = snapshot.get("worker_process", {})
        worker_dead = None
        if worker.get("platform") == "windows" and worker.get("created") is not None:
            try:
                worker_dead = process_identity(worker["pid"]) != worker["created"]
            except OSError:
                worker_dead = None
        command_safe = all(
            row["state"] in {"stopped", "isolated"}
            and json.loads(row["evidence_json"] or "{}").get("identity_verified")
            and json.loads(row["evidence_json"] or "{}").get("stopped")
            for row in database["commands"]
        )
        active_intents = [row for row in database["reservations"] if row["state"] == "dispatched"]
        requests_safe = all(
            worker_dead is True
            or (
                row["purpose"] == "tool"
                and command_safe
                and any(
                    json.loads(command["identity_json"]).get("reservation_id") == row["id"]
                    for command in database["commands"]
                )
            )
            for row in active_intents
        )
        remote_safe = not any(
            row["state"] in {"intent", "unknown"} for row in database["operations"]
        )
        owns_lease = lease["holder_run_id"] == database["authority_run_id"]
        lease_expired = owns_lease and lease["expires_at"] <= now
        lease_safe = lease["holder_run_id"] is None or (
            owns_lease and (lease_expired or worker_dead is True)
        )
        artifact_checks = [
            {
                "artifact_id": artifact["id"],
                "available": bool(artifact["available"]),
                "same_run": artifact["run_id"] in {member["id"] for member in database["family"]},
                **verify_file(
                    self.settings.artifacts_dir, artifact["relative_path"], artifact["sha256"]
                ),
            }
            for artifact in artifacts
        ]
        checkpoints = []
        expected = [
            (stage["thread_id"], stage["checkpoint_id"])
            for stage in database["stages"]
            if stage["checkpoint_id"] is not None
        ]
        if expected:
            try:
                with sqlite3.connect(
                    self.settings.checkpoint_database.as_uri() + "?mode=ro", uri=True
                ) as connection:
                    for thread_id, checkpoint_id in expected:
                        exists = (
                            connection.execute(
                                "SELECT 1 FROM checkpoints WHERE thread_id=? AND checkpoint_id=?",
                                (thread_id, checkpoint_id),
                            ).fetchone()
                            is not None
                        )
                        checkpoints.append(
                            {
                                "thread_id": thread_id,
                                "checkpoint_id": checkpoint_id,
                                "exists": exists,
                            }
                        )
            except sqlite3.Error:
                checkpoints = [
                    {"thread_id": thread, "checkpoint_id": checkpoint, "exists": False}
                    for thread, checkpoint in expected
                ]
        same_snapshot = (
            snapshot.get("input_revision") == task["input_revision"]
            and snapshot.get("feedback_revision") == task["feedback_revision"]
            and snapshot.get("acceptance_revision") == task["acceptance_revision"]
            and snapshot.get("configuration") == json.loads(task["configuration_json"])
            and snapshot.get("target_sha") == (repository["target_sha"] if repository else None)
            and snapshot.get("target_revision")
            == (repository["target_revision"] if repository else None)
        )
        normal_wait = (
            run["state"] in {"suspended", "ended", "abandoned"}
            and not owns_lease
            and task["state"] in {"waiting", "ended", "cancelled", "queued"}
        )
        waiting_stages = {
            stage["id"] for stage in database["runtime_stages"] if stage["state"] == "waiting"
        }
        settled_members = {
            member["id"]
            for member in database["family"]
            if member["state"] in {"suspended", "ended", "abandoned"}
        }
        stage_unknown = (
            any(
                stage["state"] == "intent"
                and not (stage["id"] in waiting_stages and stage["run_id"] in settled_members)
                for stage in database["stages"]
            )
            or any(intent["state"] == "pending" for intent in database["artifact_intents"])
            or any(
                invocation["state"] in {"pending", "unknown"}
                for invocation in database["provider_invocations"]
            )
        )
        safe_to_abandon = command_safe and requests_safe and remote_safe and lease_safe
        if run["state"] == "running" and run["parent_run_id"] is not None:
            safe_to_abandon = False
        facts = {
            "database_sha256": content_digest(database),
            "worker_dead": worker_dead,
            "command_safe": command_safe,
            "requests_safe": requests_safe,
            "remote_safe": remote_safe,
            "lease_expired": lease_expired,
            "normal_wait": normal_wait,
            "same_snapshot": same_snapshot,
            "artifacts": artifact_checks,
            "checkpoints": checkpoints,
            "stage_result_unknown": stage_unknown,
            "safe_to_abandon": safe_to_abandon,
            "safe_to_reuse_checkpoint": bool(expected)
            and same_snapshot
            and not stage_unknown
            and not any(stage["state"] == "intent" for stage in database["stages"])
            and all(
                row["valid"] and row["available"] and row["same_run"] for row in artifact_checks
            )
            and all(row["exists"] for row in checkpoints),
            "database": database,
        }
        artifacts_safe = all(
            row["valid"] and row["available"] and row["same_run"] for row in artifact_checks
        )
        state = (
            "safe_wait"
            if normal_wait and safe_to_abandon and not stage_unknown and artifacts_safe
            else "review_required"
            if safe_to_abandon
            else "unsafe"
        )
        basis = content_digest(facts)
        identifier = stable_id("reconciliation", run_id, basis)
        with self.store.write() as records:
            if content_digest(_database_basis(records, run_id)) != facts["database_sha256"]:
                raise ConflictError("核对期间业务状态变化，请重新读取")
            if records.one("reconciliation_reports", id=identifier) is None:
                records.add(
                    "reconciliation_reports",
                    {
                        "id": identifier,
                        "run_id": run_id,
                        "basis_sha256": basis,
                        "facts_json": canonical_json(facts),
                        "state": state,
                        "created_at": now,
                    },
                )
        return {
            "id": identifier,
            "run_id": run_id,
            "basis_sha256": basis,
            "state": state,
            "facts": facts,
        }

    def scan_startup(self, *, now: float) -> list[dict]:
        with self.store.read() as records:
            run_ids = [row["id"] for row in records.find("runs")]
        reports = [self.report(run_id, now=now) for run_id in run_ids]
        with self.store.write() as records:
            for report in reports:
                facts = report["facts"]
                run = facts["database"]["run"]
                if report["state"] == "safe_wait":
                    continue
                if (
                    run["state"] == "running"
                    and facts["worker_dead"] is False
                    and not facts["lease_expired"]
                    and facts["database"]["lease"]["holder_run_id"]
                    == facts["database"]["authority_run_id"]
                ):
                    continue  # 健康的另一 Worker 仍由执行位阻止双领取。
                if (
                    run["state"] == "running"
                    or not facts["remote_safe"]
                    or not facts["command_safe"]
                    or facts["stage_result_unknown"]
                ):
                    reason_id = stable_id("block", run["task_id"], "abandoned_run")
                    if records.one("block_reasons", id=reason_id) is None:
                        block(
                            records,
                            "abandoned_run",
                            run["task_id"],
                            now=now,
                            report_id=report["id"],
                        )
                elif any(
                    not artifact["valid"] or not artifact["available"]
                    for artifact in facts["artifacts"]
                ):
                    identifier = stable_id("block", run["task_id"], "artifact_missing")
                    if records.one("block_reasons", id=identifier) is None:
                        block(
                            records,
                            "artifact_missing",
                            run["task_id"],
                            now=now,
                            report_id=report["id"],
                        )
        return reports

    def abandon(
        self,
        operation_id,
        report_id,
        *,
        expected_basis: str,
        now: float,
        explanation: str,
        actor="local-user",
    ):
        human(actor)
        if not explanation.strip():
            raise ValueError("恢复必须说明核对依据")
        request = {
            "kind": "abandon_old_execution",
            "report": report_id,
            "basis": expected_basis,
            "explanation": explanation,
            "actor": actor,
        }
        with self.store.read() as records:
            _, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            saved = required(records, "reconciliation_reports", id=report_id)
            run_id = saved["run_id"]
        current = self.report(run_id, now=now)
        if current["basis_sha256"] != expected_basis:
            raise ConflictError("恢复报告已过期，请重新核对")
        if not current["facts"]["safe_to_abandon"]:
            raise PermissionError("旧命令、请求或远端副作用仍未核实")
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            database = _database_basis(records, run_id)
            if content_digest(database) != current["facts"]["database_sha256"]:
                raise ConflictError("核对后业务记录又变化")
            assert_quiescent(
                records, excluding_runs=[member["id"] for member in database["family"]]
            )
            run, task, lease = (database[key] for key in ("run", "task", "lease"))
            if run["state"] != "running":
                raise ConflictError("此运行不是异常执行中记录")
            ledger = required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])
            _, _, held = ledger_values(ledger)
            for reservation in database["reservations"]:
                if reservation["state"] == "reserved":
                    outstanding = vector(json.loads(reservation["outstanding_json"]))
                    held = {key: held[key] - outstanding[key] for key in DIMENSIONS}
                    records.change(
                        "budget_reservations",
                        {"id": reservation["id"]},
                        {
                            "state": "cancelled",
                            "outstanding_json": canonical_json(vector({})),
                            "finished_at": now,
                        },
                    )
                elif reservation["state"] == "dispatched":
                    records.change(
                        "budget_reservations",
                        {"id": reservation["id"]},
                        {"state": "unknown", "finished_at": now},
                    )
            # 仅预留旧租期内未计量的保守时长，避免把停机等待当成实际用时。
            previous = (
                run["budget_metered_ms"]
                if run["budget_metered_ms"] is not None
                else round((run["started_at"] if run["started_at"] is not None else now) * 1000)
            )
            limits, used, _ = ledger_values(ledger)
            if lease["holder_run_id"] == run_id and lease["expires_at"] is not None:
                upper_time = min(now, lease["expires_at"])
                unknown_time = max(0, round(upper_time * 1000) - previous) if limits else 0
            else:
                unknown_time = max(0, limits.get("active_ms", 0) - used["active_ms"])
            if run["budget_clock_state"] == "backoff":
                unknown_time = 0
            already = sum(
                json.loads(row["outstanding_json"]).get("active_ms", 0)
                for row in database["reservations"]
                if row["state"] in {"dispatched", "unknown"}
            )
            extra = vector({"active_ms": max(0, unknown_time - already)})
            if any(extra.values()) and records.one("recovery_holds", run_id=run_id) is None:
                held = plus(held, extra)
                records.add(
                    "recovery_holds",
                    {
                        "run_id": run_id,
                        "owner_task_id": run["budget_owner_task_id"],
                        "amounts_json": canonical_json(extra),
                        "evidence_json": canonical_json({"report_id": report_id}),
                        "state": "held",
                    },
                )
            save_ledger(records, ledger, reserved=held)
            for member in database["family"]:
                if member["id"] != run_id and member["state"] == "running":
                    records.change(
                        "runs",
                        {"id": member["id"]},
                        {"state": "abandoned", "ended_at": now, "active_child_run_id": None},
                    )
            for intent in database["artifact_intents"]:
                if intent["state"] == "pending":
                    records.change(
                        "artifact_intents", {"id": intent["id"]}, {"state": "unavailable"}
                    )
            records.change(
                "runs",
                {"id": run_id},
                {
                    "state": "abandoned",
                    "wait_reason": "explicit_reconciliation",
                    "ended_at": now,
                    "active_child_run_id": None,
                },
            )
            if task["cancel_requested"] or task["user_excluded"]:
                bump_task(records, task, state="cancelled", wait_reason=None)
            else:
                bump_task(records, task, state="blocked", wait_reason=None, outcome="abandoned_run")
            if lease["holder_run_id"] == run_id:
                records.change(
                    "execution_lease",
                    {"singleton": 1},
                    {"holder_run_id": None, "expires_at": None, "heartbeat_at": None},
                )
            reason_id = stable_id("block", task["id"], "abandoned_run")
            if records.one("block_reasons", id=reason_id) is None:
                block(records, "abandoned_run", task["id"], now=now, report_id=report_id)
            result = {
                "run_id": run_id,
                "state": "abandoned",
                "global_paused": True,
                "safe_to_reuse_checkpoint": current["facts"]["safe_to_reuse_checkpoint"],
            }
            remember_operation(records, operation_id, fingerprint, 1, result)
            business_event(
                records,
                "old_execution_abandoned",
                task["id"],
                operation_id=operation_id,
                report_id=report_id,
                explanation=explanation,
                actor=actor,
            )
            return result
