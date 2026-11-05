"""用户指定的有限诊断；不构造 GitHub 身份，不恢复普通队列。"""

import json
import math
from uuid import uuid4

from repowatch.application.budget_accounting import save_ledger
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.execution_policy import DIAGNOSTIC_POLICY_REVISION, DIAGNOSTIC_PURPOSE
from repowatch.application.orchestration import _finish
from repowatch.application.safety import assert_quiescent, assert_task_budget
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import (
    bump_task,
    business_event,
    capture_run,
    create_task,
    execution_record,
    required,
)
from repowatch.domain.budget import BudgetAmounts, amounts
from repowatch.domain.task_records import ExecutionToken, canonical_json, require_time

POLICY_REVISION = DIAGNOSTIC_POLICY_REVISION


def validate_parameters(kind, parameters):
    if kind not in DIAGNOSTIC_PURPOSE or not isinstance(parameters, dict):
        raise ValueError("未知诊断类型")
    expected = set() if kind == "storage_probe" else {"provider_revision_id", "model"}
    optional = set()
    if kind == "provider_capability":
        expected.add("role")
        optional.add("reasoning_effort")
        if parameters.get("role") not in {"processor", "reviewer"}:
            raise ValueError("未知诊断角色")
    if (
        not expected <= set(parameters)
        or set(parameters) - expected - optional
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 200
            for value in parameters.values()
        )
    ):
        raise ValueError("诊断只接收已保存配置引用和模型名，不接收凭据或命令")


class DiagnosticService:
    def __init__(self, store):
        self.store = store

    def expire_queued(self, *, now):
        require_time(now)
        with self.store.write() as records:
            for permission in records.find("diagnostic_authorizations", state="queued"):
                task = required(records, "tasks", id=permission["task_id"])
                if (
                    permission["deadline"] > now
                    and permission["policy_revision"] == POLICY_REVISION
                    and not task["cancel_requested"]
                    and not task["user_excluded"]
                ):
                    continue
                records.change(
                    "diagnostic_authorizations",
                    {"task_id": task["id"]},
                    {
                        "state": "failed",
                        "result_json": canonical_json(
                            {"passed": False, "checks": {}, "reason": "expired_or_cancelled"}
                        ),
                    },
                )
                if task["state"] == "queued":
                    bump_task(records, task, state="ended", outcome="diagnostic_expired")
                business_event(records, "diagnostic_expired", task["id"])

    def request(
        self,
        operation_id,
        kind,
        parameters,
        limits: BudgetAmounts,
        *,
        expected_revision,
        now,
        duration=60,
        actor="local-user",
    ):
        human(actor)
        require_time(now)
        validate_parameters(kind, parameters)
        totals = amounts(limits.as_dict(), complete=True, positive=True)
        if (
            not math.isfinite(duration)
            or not 0 < duration <= 300
            or totals["active_ms"] > 300_000
            or totals["model_calls"] > 10
            or totals["tokens"] > 100_000
            or totals["repair_rounds"] > 1
            or totals["recovery_rounds"] > 1
        ):
            raise ValueError("诊断必须使用有限的小额预算及不超过 300 秒的期限")
        request = {
            "kind": "diagnostic",
            "test": kind,
            "parameters": parameters,
            "limits": totals,
            "duration": duration,
            "revision": expected_revision,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            if required(records, "global_control", singleton=1)["revision"] != expected_revision:
                raise ConflictError("控制状态已变化，请刷新")
            task_id = create_task(
                records,
                None,
                "diagnostic:" + operation_id,
                None,
                "diagnose",
                now,
                input_payload={"kind": kind, "parameters": parameters},
            )
            ledger = required(records, "budget_ledgers", owner_task_id=task_id)
            save_ledger(
                records,
                ledger,
                limits_json=canonical_json(totals),
                initial_limits_json=canonical_json(totals),
                authorization_revision=2,
            )
            result = {"task_id": task_id, "state": "queued", "deadline": now + duration}
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            records.add(
                "diagnostic_authorizations",
                {
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "policy_revision": POLICY_REVISION,
                    "kind": kind,
                    "parameters_json": canonical_json(parameters),
                    "deadline": now + duration,
                    "state": "queued",
                },
            )
            business_event(records, "diagnostic_requested", task_id, diagnostic_kind=kind)
            return result

    def claim(self, task_id, *, now) -> ExecutionToken:
        self.store.assert_execution_available()
        require_time(now)
        with self.store.write() as records:
            task = required(records, "tasks", id=task_id)
            permission = required(records, "diagnostic_authorizations", task_id=task_id)
            if (
                task["mode"] != "diagnose"
                or task["state"] != "queued"
                or permission["state"] != "queued"
                or permission["deadline"] <= now
                or task["stop_requested"]
                or task["cancel_requested"]
                or task["user_excluded"]
                or permission["policy_revision"] != POLICY_REVISION
            ):
                raise PermissionError("诊断授权无效、已领取、到期或已取消")
            # Unknown publication remains frozen; it cannot prevent a read-only provider test.
            assert_quiescent(records, allow_unknown_remote=True)
            assert_task_budget(records, task)
            lease = required(records, "execution_lease", singleton=1)
            token = ExecutionToken(str(uuid4()), lease["generation"] + 1)
            capture_run(records, task, token.run_id, token.generation, now)
            bump_task(records, task, state="running", wait_reason=None)
            records.change(
                "diagnostic_authorizations",
                {"task_id": task_id},
                {
                    "run_id": token.run_id,
                    "state": "running",
                },
            )
            records.change(
                "execution_lease",
                {"singleton": 1},
                {
                    "holder_run_id": token.run_id,
                    "generation": token.generation,
                    "expires_at": min(now + 30, permission["deadline"]),
                    "heartbeat_at": now,
                },
            )
            business_event(records, "diagnostic_started", task_id, run_id=token.run_id)
            return token

    def specification(self, token, *, now):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            permission = required(records, "diagnostic_authorizations", task_id=run["task_id"])
            if permission["state"] != "running" or permission["run_id"] != token.run_id:
                raise PermissionError("不是当前诊断")
            return {
                "kind": permission["kind"],
                "parameters": json.loads(permission["parameters_json"]),
                "deadline": permission["deadline"],
                "purpose": DIAGNOSTIC_PURPOSE[permission["kind"]],
            }

    def finish(self, token, *, now, passed, checks):
        # Persist only enumerated boolean facts, never arbitrary SDK response/error text.
        if (
            type(passed) is not bool
            or not isinstance(checks, dict)
            or len(checks) > 20
            or any(
                key not in {"connection", "structured_output", "tool_calling", "storage"}
                or type(value) is not bool
                for key, value in checks.items()
            )
        ):
            raise ValueError("诊断结果必须是受限核验事实")
        with self.store.write() as records:
            run = execution_record(records, token, now)
            permission = required(records, "diagnostic_authorizations", task_id=run["task_id"])
            if permission["run_id"] != token.run_id or permission["state"] != "running":
                raise PermissionError("诊断结果归属不匹配")
            if now >= permission["deadline"]:
                passed = False
            task = required(records, "tasks", id=run["task_id"])
            if task["budget_wait_reason"] in {"budget_renewal", "budget_configuration"}:
                bump_task(records, task, budget_wait_reason=None)
                task = required(records, "tasks", id=run["task_id"])
            if task["cancel_requested"] or task["user_excluded"]:
                passed = False
            _finish(records, token, now, state="ended", outcome="diagnostic_result")
            result = {"passed": passed, "checks": checks}
            records.change(
                "diagnostic_authorizations",
                {"task_id": run["task_id"]},
                {
                    "state": "completed" if passed else "failed",
                    "result_json": canonical_json(result),
                },
            )
            business_event(records, "diagnostic_finished", run["task_id"], **result)
            return result
