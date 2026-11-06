"""业务队列与执行权。所有操作均为短事务，模型与命令在事务外执行。"""

import json
import math
from uuid import uuid4

from repowatch.application.budget_accounting import (
    ledger_values,
    meter_execution,
    reservation_guard,
)
from repowatch.application.execution_policy import launch_permitted
from repowatch.application.queue_state import is_ready, refresh_ready, settle_ready
from repowatch.application.safety import assert_quiescent
from repowatch.application.storage import BusinessStore, ConflictError
from repowatch.application.task_records import (
    block,
    bump_task,
    business_event,
    capture_run,
    create_task,
    execution_record,
    required,
)
from repowatch.domain.budget import is_exhausted
from repowatch.domain.task_records import (
    ExecutionToken,
    canonical_json,
    content_digest,
    require_time,
)


def commands_clear(records, run_id):
    if any(
        command["state"] not in {"stopped", "isolated"}
        for command in records.find("command_states", run_id=run_id)
    ):
        raise PermissionError("旧命令尚未确认停止或隔离，不能释放执行位")


def _finish(records, token, now, *, state, wait_reason=None, outcome=None, use_transition=True):
    run = execution_record(records, token, now)
    if run["active_child_run_id"] is not None:
        raise PermissionError("子任务尚未安全结束，父运行不能释放")
    commands_clear(records, token.run_id)
    if any(
        item["state"] in {"reserved", "dispatched"}
        for item in records.find("budget_reservations", run_id=run["id"])
    ):
        raise PermissionError("预算步骤仍未核销或撤销，不能释放执行位")
    ledger = meter_execution(records, run, now)
    if token.authority_run_id is not None:
        parent = required(records, "runs", id=run["parent_run_id"])
        records.change(
            "runs",
            {"id": run["id"]},
            {
                "state": "ended",
                "ended_at": now,
                "wait_reason": wait_reason or outcome,
            },
        )
        records.change("runs", {"id": parent["id"]}, {"active_child_run_id": None})
        business_event(records, "child_run_finished", run["task_id"], run_id=run["id"])
        return
    task = required(records, "tasks", id=run["task_id"])
    repository = (
        required(records, "repositories", id=task["repository_id"])
        if task["repository_id"] is not None
        else None
    )
    settle_ready(records, now)
    mode = task["mode"]
    if task["cancel_requested"]:
        state, wait_reason, outcome = "cancelled", None, "cancelled"
    elif task["user_excluded"]:
        state, wait_reason, outcome = "ended", None, "user_excluded"
    elif required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])["revoked"]:
        state, wait_reason, outcome = "blocked", None, "budget_revoked"
    elif is_exhausted(*ledger_values(ledger)[:2]):
        state, wait_reason, outcome = "blocked", None, "budget_exhausted"
    elif task["budget_wait_reason"] in {"budget_bound_exceeded", "progress_evidence_unavailable"}:
        state, wait_reason, outcome = "blocked", None, task["budget_wait_reason"]
    elif state != "blocked" and task["budget_wait_reason"] in {
        "budget_renewal",
        "budget_configuration",
    }:
        state, wait_reason = "waiting", task["budget_wait_reason"]
    elif task["stop_requested"] and records.find("scan_dependencies", waiting_task_id=task["id"]):
        state, wait_reason = "waiting", "scan"
    elif task["pending_transition_json"] and use_transition:
        transition = json.loads(task["pending_transition_json"])
        if (
            transition["input_revision"] == task["input_revision"]
            and transition["feedback_revision"] == task["feedback_revision"]
            and transition["admission_revision"] == repository["admission_revision"]
            and transition.get("acceptance_revision", task["acceptance_revision"])
            == task["acceptance_revision"]
        ):
            state, wait_reason, outcome, mode = (
                transition[key] for key in ("state", "wait_reason", "outcome", "mode")
            )
        else:
            state, wait_reason, outcome, mode = "queued", None, None, "triage"
    elif task["stop_requested"]:
        state = "waiting"
        wait_reason = (
            "scan"
            if records.find("scan_dependencies", waiting_task_id=task["id"])
            else task["wait_reason"] or "safe_stop"
        )
    elif (
        state != "blocked"
        and task["mode"] == "triage"
        and (
            task["input_revision"] != run["input_revision"]
            or task["feedback_revision"] != run["feedback_revision"]
            or repository["admission_revision"] != run["admission_revision"]
        )
    ):
        state, wait_reason, outcome, mode = "queued", None, None, "triage"
    if state not in {"queued", "waiting", "blocked", "ended", "cancelled"}:
        raise ValueError("未知运行结束状态")
    if state == "waiting" and not wait_reason:
        raise ValueError("正常等待必须说明原因")
    if state == "blocked" and not records.find("block_reasons", task_id=task["id"], resolved=0):
        block(records, outcome or "execution_blocked", task["id"], now=now)
    bump_task(
        records,
        task,
        state=state,
        wait_reason=wait_reason,
        outcome=outcome,
        mode=mode,
        pending_transition_json=None,
        stop_requested=0,
    )
    records.change(
        "runs",
        {"id": token.run_id},
        {
            "state": "suspended" if state in {"queued", "waiting", "blocked"} else "ended",
            "wait_reason": wait_reason or ("continuation" if state == "queued" else state),
            "ended_at": now,
        },
    )
    records.change(
        "execution_lease",
        {"singleton": 1},
        {
            "holder_run_id": None,
            "expires_at": None,
            "heartbeat_at": None,
        },
    )
    business_event(
        records,
        "run_finished",
        task["id"],
        run_id=token.run_id,
        state=state,
        wait_reason=wait_reason,
        outcome=outcome,
    )
    refresh_ready(records, now)


class Orchestrator:
    def __init__(
        self, store: BusinessStore, *, aging_interval=86_400, max_auto_priority=4, target_gate=None
    ):
        if (
            not math.isfinite(aging_interval)
            or aging_interval <= 0
            or not 0 <= max_auto_priority <= 4
        ):
            raise ValueError("老化间隔与自动优先级上限无效")
        self.store = store
        self.aging_interval = aging_interval
        self.max_auto_priority = max_auto_priority
        self.target_gate = target_gate

    def claim(
        self, *, now: float, ttl=30, run_id: str | None = None, snapshot: dict | None = None
    ) -> ExecutionToken | None:
        require_time(now)
        self.store.assert_execution_available()
        if not math.isfinite(ttl) or not 0 < ttl <= 300:
            raise ValueError("执行租期必须在 0—300 秒内")
        with self.store.write() as records:
            settle_ready(records, now)
            refresh_ready(records, now)
            lease = required(records, "execution_lease", singleton=1)
            if lease["holder_run_id"] is not None:
                if lease["expires_at"] <= now:
                    old = required(records, "runs", id=lease["holder_run_id"])
                    block(records, "lease_expired", old["task_id"], now=now, run_id=old["id"])
                return None
            orphaned = records.find("runs", state="running")
            if orphaned:
                block(
                    records,
                    "abandoned_run",
                    orphaned[0]["task_id"],
                    now=now,
                    run_id=orphaned[0]["id"],
                )
                return None
            pending = records.find("artifact_intents", state="pending")
            if pending:
                old = required(records, "runs", id=pending[0]["run_id"])
                block(records, "artifact_persistence_pending", old["task_id"], now=now)
                return None
            candidates = [task for task in records.find("tasks") if is_ready(records, task)]
            if not candidates:
                return None

            def ordering(task):
                manual = task["manual_priority"]
                automatic = min(
                    self.max_auto_priority,
                    task["severity"] + int(task["ready_seconds"] // self.aging_interval),
                )
                return (
                    0 if manual is not None else 1,
                    -manual if manual is not None else -automatic,
                    task["queued_at"],
                    task["id"],
                )

            task = min(candidates, key=ordering)
            token = ExecutionToken(run_id or str(uuid4()), lease["generation"] + 1)
            run_snapshot = dict(snapshot or {})
            run_snapshot.pop("retry_authorization", None)
            permits = [
                permit
                for permit in records.find(
                    "retry_authorizations", task_id=task["id"], consumed_by_run_id=None
                )
                if permit["configuration_sha256"]
                == content_digest(json.loads(task["configuration_json"]))
                and required(records, "block_reasons", id=permit["reason_id"])["resolved"]
            ]
            if permits:
                permit = permits[-1]
                run_snapshot["retry_authorization"] = {
                    "id": permit["id"],
                    "plan": json.loads(permit["plan_json"]),
                    "limits": json.loads(permit["limits_json"]),
                }
            capture_run(records, task, token.run_id, token.generation, now, snapshot=run_snapshot)
            if permits:
                records.change(
                    "retry_authorizations",
                    {"id": permits[-1]["id"]},
                    {"consumed_by_run_id": token.run_id},
                )
            bump_task(records, task, state="running", wait_reason=None, ready_since=None)
            records.change(
                "execution_lease",
                {"singleton": 1},
                {
                    "holder_run_id": token.run_id,
                    "generation": token.generation,
                    "expires_at": now + ttl,
                    "heartbeat_at": now,
                },
            )
            business_event(
                records, "run_claimed", task["id"], run_id=token.run_id, generation=token.generation
            )
            return token

    def guard(
        self,
        token: ExecutionToken,
        *,
        now: float,
        purpose: str = "tool",
        reservation_id: str | None = None,
    ) -> dict:
        if purpose not in {"tool", "model", "publish", "counter", "result", "boundary"}:
            raise ValueError("未知执行用途")
        if purpose != "result":
            self.store.assert_execution_available()
        if purpose == "publish":
            with self.store.read() as records:
                run = execution_record(records, token, now)
                task = required(records, "tasks", id=run["task_id"])
                repository = (
                    required(records, "repositories", id=task["repository_id"])
                    if task["repository_id"] is not None
                    else None
                )
                requires_target = repository is not None and repository["identity_json"] is not None
            if requires_target and (
                self.target_gate is None or not self.target_gate(token, now=now)
            ):
                raise PermissionError("发布必须无条件读取当前目标并通过扫描就绪核验")
        denied = False
        with self.store.write() as records:
            run = execution_record(records, token, now)
            if purpose != "result":
                budget = meter_execution(records, run, now)
                task = required(records, "tasks", id=run["task_id"])
                budget = required(
                    records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"]
                )
                limits, used, _ = ledger_values(budget)
                denied = (
                    not launch_permitted(records, run, now=now, purpose=purpose)
                    or task["stop_requested"]
                    or task["cancel_requested"]
                    or task["user_excluded"]
                    or budget["revoked"]
                    or not limits
                    or is_exhausted(limits, used)
                )
                if purpose == "publish" and task["repository_id"] is not None:
                    from repowatch.domain.scans import readiness

                    repository = required(records, "repositories", id=task["repository_id"])
                    if repository["identity_json"] is not None:
                        batch = (
                            records.one("scan_batches", id=repository["scan_ready_batch_id"])
                            if repository["scan_ready_batch_id"]
                            else None
                        )
                        denied = denied or (
                            json.loads(repository["scan_ready_json"] or "null")
                            != readiness(repository)
                            or task["issue_id"] is None
                            or bool(records.find("scan_dependencies", waiting_task_id=task["id"]))
                            or batch is None
                            or batch["state"] != "completed"
                        )
                if not denied and purpose != "boundary":
                    reservation_guard(records, run, reservation_id, purpose)
            result = dict(run)
        if denied:
            raise PermissionError("暂停、取消、排除或预算未授权，不能发起新执行")
        return result

    def heartbeat(self, token: ExecutionToken, *, now: float, ttl=30):
        if not math.isfinite(ttl) or not 0 < ttl <= 300:
            raise ValueError("执行租期必须在 0—300 秒内")
        with self.store.write() as records:
            run = execution_record(records, token, now)
            meter_execution(records, run, now)
            records.change(
                "execution_lease",
                {"singleton": 1},
                {
                    "expires_at": now + ttl,
                    "heartbeat_at": now,
                },
            )

    def complete(
        self, token: ExecutionToken, *, now: float, state="ended", wait_reason=None, outcome=None
    ):
        with self.store.write() as records:
            _finish(records, token, now, state=state, wait_reason=wait_reason, outcome=outcome)

    def set_pause(self, paused: bool, *, now: float, repository_id: str | None = None):
        with self.store.write() as records:
            settle_ready(records, now)
            collection = "repositories" if repository_id else "global_control"
            identity = {"id": repository_id} if repository_id else {"singleton": 1}
            row = required(records, collection, **identity)
            if not paused and records.find("block_reasons", resolved=0):
                raise PermissionError("未解决阻塞需要显式核对，不能直接解除暂停")
            if not paused:
                assert_quiescent(records)
            records.change(
                collection, identity, {"paused": int(paused), "revision": row["revision"] + 1}
            )
            business_event(
                records, "pause_changed", repository_id=repository_id, paused=bool(paused)
            )
            refresh_ready(records, now)

    def set_priority(self, task_id: str, *, now: float, manual: int | None = None, severity=0):
        if (
            manual is not None
            and (type(manual) is not int or not -1000 <= manual <= 1000)
            or type(severity) is not int
            or not 0 <= severity <= 4
        ):
            raise ValueError("优先级超出允许范围")
        with self.store.write() as records:
            settle_ready(records, now)
            task = required(records, "tasks", id=task_id)
            bump_task(records, task, manual_priority=manual, severity=severity)
            business_event(records, "priority_changed", task_id, manual=manual, severity=severity)
            refresh_ready(records, now)

    def add_scan_dependency(self, waiting_task_id: str, scan_task_id: str, *, now: float):
        with self.store.write() as records:
            task = required(records, "tasks", id=waiting_task_id)
            if task["state"] == "running":
                raise PermissionError("活跃运行须通过 wait_for_scan 原子交接")
            self._add_dependency(records, task, scan_task_id, now)

    @staticmethod
    def _add_dependency(records, task, scan_task_id, now):
        if (
            task["user_excluded"]
            or task["cancel_requested"]
            or task["state"]
            in {
                "ended",
                "cancelled",
                "blocked",
            }
        ):
            raise ConflictError("已排除、取消或阻塞的任务不能自动增加依赖")
        scan = required(records, "tasks", id=scan_task_id)
        if (
            scan["mode"] != "scan"
            or scan["repository_id"] != task["repository_id"]
            or scan["state"] in {"ended", "cancelled"}
            or scan_task_id == task["id"]
        ):
            raise ConflictError("扫描依赖的身份、模式或状态不匹配")
        settle_ready(records, now)
        if (
            records.one("scan_dependencies", waiting_task_id=task["id"], scan_task_id=scan_task_id)
            is None
        ):
            records.add(
                "scan_dependencies", {"waiting_task_id": task["id"], "scan_task_id": scan_task_id}
            )
        if task["state"] == "queued":
            bump_task(records, task, state="waiting", wait_reason="scan")
        refresh_ready(records, now)

    def wait_for_scan(
        self, token: ExecutionToken, *, now: float, existing_scan_id: str | None = None
    ) -> str:
        with self.store.write() as records:
            run = execution_record(records, token, now)
            commands_clear(records, token.run_id)
            task = required(records, "tasks", id=run["task_id"])
            repository = required(records, "repositories", id=task["repository_id"])
            if not repository["target_sha"]:
                raise ConflictError("目标 SHA 未确认，不能创建前置扫描")
            if existing_scan_id:
                scan_id = existing_scan_id
            else:
                active = [
                    row
                    for row in records.find("tasks", repository_id=repository["id"], mode="scan")
                    if row["id"] != task["id"]
                    and row["state"] not in {"ended", "cancelled"}
                    and all(
                        json.loads(row["input_json"]).get(key) == repository[key]
                        for key in (
                            "target_sha",
                            "target_revision",
                            "import_epoch",
                            "scan_policy_revision",
                        )
                    )
                ]
                source_key = (
                    f"prerequisite-scan:{repository['target_revision']}:{repository['import_epoch']}:"
                    f"{repository['scan_policy_revision']}:{repository['target_sha']}"
                )
                source = records.one(
                    "task_sources", repository_id=task["repository_id"], source_key=source_key
                )
                if active:
                    scan_id = active[0]["id"]
                elif source and required(records, "tasks", id=source["task_id"])["state"] not in {
                    "ended",
                    "cancelled",
                }:
                    scan_id = source["task_id"]
                else:
                    if source:
                        source_key += ":revalidate:" + run["id"]
                    scan_id = create_task(
                        records,
                        task["repository_id"],
                        source_key,
                        None,
                        "scan",
                        now,
                        owner=run["budget_owner_task_id"],
                        input_payload={
                            "target_sha": repository["target_sha"],
                            "target_revision": repository["target_revision"],
                            "import_epoch": repository["import_epoch"],
                            "scan_policy_revision": repository["scan_policy_revision"],
                        },
                    )
                    records.change(
                        "tasks",
                        {"id": scan_id},
                        {
                            "configuration_json": task["configuration_json"],
                        },
                    )
            self._add_dependency(records, task, scan_id, now)
            _finish(records, token, now, state="waiting", wait_reason="scan", use_transition=False)
            return scan_id

    def complete_scan(
        self, token: ExecutionToken, *, now: float, validated_sha: str, batch_id=None
    ):
        """受信任扫描协调器提交已验证结果；模型输出不能直接调用本端口。"""
        from repowatch.domain.scans import readiness

        with self.store.read() as records:
            run = execution_record(records, token, now)
            task = required(records, "tasks", id=run["task_id"])
            repository = required(records, "repositories", id=task["repository_id"])
        if repository["identity_json"] is not None:
            if batch_id is None:
                raise PermissionError("导入仓库必须通过覆盖账本核验完成")
            from repowatch.application.artifacts import ArtifactService
            from repowatch.application.scans import ScanService

            batch = ScanService(
                self.store, None, ArtifactService(self.store, self.store.settings.artifacts_dir)
            ).validate_complete(token, batch_id, now=now)
            if json.loads(batch["version_json"]) != readiness(repository):
                raise ConflictError("扫描批次不属于当前导入／策略／目标版本")
        with self.store.write() as records:
            run = execution_record(records, token, now)
            task = required(records, "tasks", id=run["task_id"])
            if task["mode"] != "scan":
                raise PermissionError("当前不是扫描运行")
            budget = required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])
            if budget["revoked"] or task["cancel_requested"] or task["user_excluded"]:
                raise PermissionError("已撤销授权的扫描不能完成依赖交接")
            repository = required(records, "repositories", id=task["repository_id"])
            snapshot = json.loads(run["snapshot_json"])
            if batch_id and repository["identity_json"] is not None:
                current_batch = required(records, "scan_batches", id=batch_id)
                environment = json.loads(current_batch["environment_json"])
                if json.loads(current_batch["version_json"]) != readiness(
                    repository
                ) or environment.get("configuration_sha256") != content_digest(
                    json.loads(task["configuration_json"])
                ):
                    raise ConflictError("完成交接期间扫描版本或环境发生变化")
            expected = snapshot["input"].get("target_sha", snapshot["target_sha"])
            if (
                not validated_sha
                or validated_sha != expected
                or validated_sha != repository["target_sha"]
                or snapshot["target_revision"] != repository["target_revision"]
                or snapshot.get("import_epoch", repository["import_epoch"])
                != repository["import_epoch"]
                or snapshot.get("scan_policy_revision", repository["scan_policy_revision"])
                != repository["scan_policy_revision"]
            ):
                raise ConflictError("扫描结果不是当前固定目标版本")
            commands_clear(records, token.run_id)
            settle_ready(records, now)
            records.change(
                "repositories",
                {"id": repository["id"]},
                {
                    "scan_ready_sha": validated_sha,
                    "scan_ready_json": canonical_json(readiness(repository)),
                    "scan_ready_batch_id": batch_id,
                    "revision": repository["revision"] + 1,
                },
            )
            if batch_id:
                records.change("scan_batches", {"id": batch_id}, {"state": "completed"})
            for dependency in records.find("scan_dependencies", scan_task_id=task["id"]):
                waiting = required(records, "tasks", id=dependency["waiting_task_id"])
                records.remove(
                    "scan_dependencies", waiting_task_id=waiting["id"], scan_task_id=task["id"]
                )
                if (
                    waiting["state"] == "waiting"
                    and waiting["wait_reason"] == "scan"
                    and not waiting["user_excluded"]
                    and not waiting["cancel_requested"]
                    and not records.find("scan_dependencies", waiting_task_id=waiting["id"])
                ):
                    bump_task(records, waiting, state="queued", wait_reason=None, stop_requested=0)
            business_event(records, "scan_dependencies_satisfied", task["id"], sha=validated_sha)
            _finish(
                records, token, now, state="ended", outcome="scan_complete", use_transition=False
            )
