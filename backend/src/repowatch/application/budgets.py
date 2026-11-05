"""累计预算、单步意图及人工有限续期；不发起模型请求。"""

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from repowatch.application.budget_accounting import (
    budget_view,
    ledger_values,
    mark_budget_block,
    meter_execution,
    save_ledger,
    warning,
)
from repowatch.application.execution_policy import launch_permitted
from repowatch.application.orchestration import _finish, commands_clear
from repowatch.application.queue_state import is_ready, refresh_ready, settle_ready
from repowatch.application.storage import BusinessStore, ConflictError
from repowatch.application.task_records import (
    bump_task,
    business_event,
    cancel_dependencies,
    execution_record,
    required,
)
from repowatch.domain.budget import (
    DIMENSIONS,
    BudgetAmounts,
    BudgetDecision,
    BudgetDenied,
    RenewalPlan,
    amounts,
    available,
    is_exhausted,
    plus,
    vector,
)
from repowatch.domain.task_records import (
    ExecutionToken,
    canonical_json,
    content_digest,
    require_time,
    stable_id,
)


class ProgressEvidenceUnavailable(ValueError):
    pass


def human(actor):
    # 本地单用户身份由控制层提供；此用例不作为 Agent 工具暴露。
    if actor != "local-user":
        raise PermissionError("只有本地用户可批准预算，Agent 不得自行授权")


def replay_operation(records, operation_id, request):
    if not operation_id:
        raise ValueError("人工操作必须有稳定标识")
    fingerprint = content_digest(request)
    existing = records.one("user_operations", id=operation_id)
    if existing:
        if existing["request_sha256"] != fingerprint:
            raise ConflictError("同一操作标识的内容不同")
        return fingerprint, json.loads(existing["result_json"])
    return fingerprint, None


def remember_operation(records, operation_id, fingerprint, revision, result):
    records.add(
        "user_operations",
        {
            "id": operation_id,
            "request_sha256": fingerprint,
            "expected_revision": revision,
            "result_json": canonical_json(result),
        },
    )


def _progress_rows(records, owner):
    rows = []
    for fact in records.find("budget_progress", owner_task_id=owner):
        artifact = required(records, "artifacts", id=fact["artifact_id"])
        run = required(records, "runs", id=fact["run_id"])
        rows.append(
            {
                "fact": dict(fact),
                "artifact": dict(artifact),
                "run_snapshot_sha256": content_digest(json.loads(run["snapshot_json"])),
            }
        )
    return rows


def _input_basis(records, task_id):
    task = required(records, "tasks", id=task_id)
    repository = (
        required(records, "repositories", id=task["repository_id"])
        if task["repository_id"] is not None
        else None
    )
    return {
        "task_id": task_id,
        "input_revision": task["input_revision"],
        "feedback_revision": task["feedback_revision"],
        "acceptance_revision": task["acceptance_revision"],
        "configuration_sha256": content_digest(json.loads(task["configuration_json"])),
        "target_revision": repository["target_revision"] if repository else None,
        "target_sha": repository["target_sha"] if repository else None,
        "admission_revision": (
            repository["admission_revision"]
            if repository
            else required(records, "diagnostic_authorizations", task_id=task_id)["policy_revision"]
        ),
    }


def _basis(records, task_id, owner):
    ledger = required(records, "budget_ledgers", owner_task_id=owner)
    return {
        "owner_task_id": owner,
        "ledger_revision": ledger["revision"],
        "requester": _input_basis(records, task_id),
        "owner": _input_basis(records, owner),
        "progress_sha256": content_digest(_progress_rows(records, owner)),
    }


class BudgetService:
    def __init__(self, store: BusinessStore, artifacts_dir: Path, *, warning_percent=80):
        if type(warning_percent) is not int or not 1 <= warning_percent <= 100:
            raise ValueError("预算预警比例必须在 1—100 之间")
        self.store = store
        self.artifacts_dir = Path(artifacts_dir).resolve()
        self.warning_percent = warning_percent

    def view(self, owner_task_id: str) -> dict:
        with self.store.read() as records:
            return budget_view(
                required(records, "budget_ledgers", owner_task_id=owner_task_id),
                warning_percent=self.warning_percent,
            )

    def authorize_initial(
        self,
        operation_id: str,
        owner_task_id: str,
        limits: BudgetAmounts,
        *,
        expected_revision: int,
        reason: str,
        now: float,
        actor="local-user",
    ) -> dict:
        human(actor)
        require_time(now)
        totals = amounts(limits.as_dict(), complete=True, positive=True)
        if not reason.strip() or expected_revision <= 0:
            raise ValueError("授权必须有依据及有效预期修订")
        request = {
            "kind": "budget_initial",
            "owner": owner_task_id,
            "limits": totals,
            "revision": expected_revision,
            "reason": reason,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            ledger = required(records, "budget_ledgers", owner_task_id=owner_task_id)
            if ledger["revision"] != expected_revision or json.loads(ledger["limits_json"]):
                raise ConflictError("已有授权或账本修订变化，请使用有限续期")
            ledger = save_ledger(
                records,
                ledger,
                limits_json=canonical_json(totals),
                initial_limits_json=canonical_json(totals),
                authorization_revision=ledger["authorization_revision"] + 1,
            )
            if is_exhausted(*ledger_values(ledger)[:2]):
                mark_budget_block(
                    records,
                    {"task_id": owner_task_id, "budget_owner_task_id": owner_task_id},
                    "budget_exhausted",
                    now,
                )
            for run in records.find("runs", budget_owner_task_id=owner_task_id, state="running"):
                records.change("runs", {"id": run["id"]}, {"budget_metered_ms": round(now * 1000)})
            result = budget_view(ledger, warning_percent=self.warning_percent)
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "budget_authorized",
                owner_task_id,
                operation_id=operation_id,
                actor=actor,
                reason=reason,
                limits=totals,
            )
            return result

    def reserve(
        self,
        token: ExecutionToken,
        operation_key: str,
        upper: BudgetAmounts,
        *,
        purpose: str,
        now: float,
    ) -> BudgetDecision:
        self.store.assert_execution_available()
        bound = upper.as_dict()
        if (
            not operation_key
            or not any(bound.values())
            or purpose not in {"model", "tool", "publish", "counter"}
        ):
            raise ValueError("预留必须有稳定操作、有效用途及非零上界")
        if purpose == "counter":
            if bound["active_ms"] or bound["tokens"] or bound["model_calls"]:
                raise ValueError("轮数记账不能代替模型或工具的逐步预留")
        elif (
            not bound["active_ms"]
            or bound["repair_rounds"]
            or bound["recovery_rounds"]
            or (purpose == "model" and (bound["model_calls"] != 1 or not bound["tokens"]))
            or (purpose != "model" and (bound["model_calls"] or bound["tokens"]))
        ):
            raise ValueError("模型／工具必须按真实单步上界预留，轮数单独记账")
        with self.store.write() as records:
            run = execution_record(records, token, now)
            ledger = meter_execution(records, run, now)
            limits, used, reserved = ledger_values(ledger)
            plan = records.one(
                "budget_step_plans",
                owner_task_id=run["budget_owner_task_id"],
                operation_key=operation_key,
            )
            if plan:
                if json.loads(plan["upper_json"]) != bound or plan["purpose"] != purpose:
                    raise ConflictError("已登记步骤上界不同，不能因额度不足缩小预留")
            else:
                records.add(
                    "budget_step_plans",
                    {
                        "owner_task_id": run["budget_owner_task_id"],
                        "operation_key": operation_key,
                        "purpose": purpose,
                        "upper_json": canonical_json(bound),
                    },
                )
            existing = records.one(
                "budget_reservations",
                owner_task_id=run["budget_owner_task_id"],
                operation_key=operation_key,
            )
            if existing:
                if json.loads(existing["upper_json"]) != bound or existing["purpose"] != purpose:
                    raise ConflictError("同一步骤的上界或用途不同，不能缩小预留后重发")
                return BudgetDecision("already_recorded", existing["id"])
            status = budget_view(ledger)["status"]
            if status in {"exhausted", "revoked", "configuration_required"}:
                if status == "configuration_required":
                    self._wait_flag(records, run["task_id"], "budget_configuration")
                return BudgetDecision(status)
            if not launch_permitted(records, run, now=now, purpose=purpose):
                return BudgetDecision("blocked")
            if any(
                item["state"] in {"reserved", "dispatched"}
                for item in records.find(
                    "budget_reservations", owner_task_id=run["budget_owner_task_id"]
                )
            ):
                raise PermissionError("先完成上一预算意图，禁止父子／重试并行消费")
            remaining = available(limits, used, reserved)
            if any(bound[key] > remaining[key] for key in DIMENSIONS):
                self._wait_flag(records, run["task_id"], "budget_renewal")
                business_event(
                    records,
                    "budget_reservation_denied",
                    run["task_id"],
                    operation_key=operation_key,
                    upper=bound,
                    available=remaining,
                )
                return BudgetDecision(
                    "renewal_required",
                    warning=warning(limits, used, reserved, self.warning_percent),
                )
            identifier = stable_id("budget-reservation", run["budget_owner_task_id"], operation_key)
            records.add(
                "budget_reservations",
                {
                    "id": identifier,
                    "owner_task_id": run["budget_owner_task_id"],
                    "run_id": run["id"],
                    "generation": token.generation,
                    "operation_key": operation_key,
                    "purpose": purpose,
                    "upper_json": canonical_json(bound),
                    "outstanding_json": canonical_json(bound),
                    "state": "reserved",
                    "created_at": now,
                },
            )
            reserved = plus(reserved, bound)
            save_ledger(records, ledger, reserved=reserved)
            business_event(
                records,
                "budget_reserved",
                run["task_id"],
                reservation_id=identifier,
                owner_task_id=run["budget_owner_task_id"],
                upper=bound,
            )
            return BudgetDecision(
                "reserved", identifier, warning(limits, used, reserved, self.warning_percent)
            )

    @staticmethod
    def _wait_flag(records, task_id, reason):
        task = required(records, "tasks", id=task_id)
        if task["budget_wait_reason"] != reason:
            bump_task(records, task, budget_wait_reason=reason)

    def start(self, token: ExecutionToken, reservation_id: str, *, now: float) -> None:
        self.store.assert_execution_available()
        denial = None
        with self.store.write() as records:
            run = execution_record(records, token, now)
            ledger = meter_execution(records, run, now)
            reservation = self._reservation(records, token, reservation_id)
            if reservation["state"] != "reserved":
                raise ConflictError("意图已启动或完成，不得重复执行")
            status = budget_view(ledger)["status"]
            limits, used, held = ledger_values(ledger)
            overcommitted = bool(limits) and any(
                used[key] + held[key] > limits[key] for key in DIMENSIONS
            )
            if (
                status != "available"
                or not launch_permitted(records, run, now=now, purpose=reservation["purpose"])
                or overcommitted
            ):
                denial = BudgetDecision(
                    status if status != "available" else "blocked", reservation_id
                )
            else:
                records.change(
                    "budget_reservations", {"id": reservation_id}, {"state": "dispatched"}
                )
                business_event(
                    records, "budget_step_started", run["task_id"], reservation_id=reservation_id
                )
        if denial:
            raise BudgetDenied(denial)

    @staticmethod
    def _reservation(records, token, identifier):
        reservation = required(records, "budget_reservations", id=identifier)
        if reservation["run_id"] != token.run_id or reservation["generation"] != token.generation:
            raise PermissionError("预算意图不属于当前运行／代次")
        return reservation

    def cancel_unstarted(self, token: ExecutionToken, reservation_id: str, *, now: float):
        with self.store.write() as records:
            run = execution_record(records, token, now)
            reservation = self._reservation(records, token, reservation_id)
            if reservation["state"] == "cancelled":
                return
            if reservation["state"] != "reserved":
                raise PermissionError("已发送的请求不能当作未执行撤销")
            ledger = meter_execution(records, run, now)
            _, _, reserved = ledger_values(ledger)
            outstanding = vector(json.loads(reservation["outstanding_json"]))
            save_ledger(
                records,
                ledger,
                reserved={key: reserved[key] - outstanding[key] for key in DIMENSIONS},
            )
            records.change(
                "budget_reservations",
                {"id": reservation_id},
                {
                    "state": "cancelled",
                    "outstanding_json": canonical_json(vector({})),
                    "finished_at": now,
                },
            )
            business_event(
                records, "budget_unstarted_cancelled", run["task_id"], reservation_id=reservation_id
            )

    def settle(
        self, token: ExecutionToken, reservation_id: str, actual: dict[str, int], *, now: float
    ) -> dict:
        actual = amounts(actual)
        if "active_ms" in actual:
            raise ValueError("有效时长由宿主持久时钟计量，不接受调用方自报")
        with self.store.write() as records:
            run = execution_record(records, token, now)
            reservation = self._reservation(records, token, reservation_id)
            upper = vector(json.loads(reservation["upper_json"]))
            if reservation["purpose"] == "model":
                actual.setdefault("model_calls", 1)
                if actual["model_calls"] < 1:
                    raise ValueError("已发送的模型尝试至少计一次请求")
            if reservation["purpose"] == "counter":
                for key in ("repair_rounds", "recovery_rounds"):
                    if upper[key]:
                        actual.setdefault(key, upper[key])
                        if actual[key] < upper[key]:
                            raise ValueError("已开始的轮数不能少计")
            if reservation["state"] not in {"dispatched", "unknown", "settled"}:
                raise ConflictError("未启动的意图不能核销为实际用量")
            previous = json.loads(reservation["actual_json"])
            if any(key in previous and previous[key] != value for key, value in actual.items()):
                raise ConflictError("已核实的实际用量不能覆盖或清零")
            if reservation["state"] in {"unknown", "settled"} and all(
                key in previous for key in actual
            ):
                return budget_view(
                    required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])
                )
            ledger = meter_execution(records, run, now)
            reservation = self._reservation(records, token, reservation_id)
            upper = vector(json.loads(reservation["upper_json"]))
            _, used, reserved = ledger_values(ledger)
            merged = {**previous, **actual, "active_ms": reservation["metered_active_ms"]}
            for key in DIMENSIONS:
                if upper[key] == 0:
                    merged.setdefault(key, 0)
                if key != "active_ms" and key not in previous and key in merged:
                    used[key] += merged[key]
            old_hold = vector(json.loads(reservation["outstanding_json"]))
            new_hold = {key: upper[key] if key not in merged else 0 for key in DIMENSIONS}
            reserved = {key: reserved[key] - old_hold[key] + new_hold[key] for key in DIMENSIONS}
            ledger = save_ledger(records, ledger, used=used, reserved=reserved)
            state = "unknown" if any(new_hold.values()) else "settled"
            records.change(
                "budget_reservations",
                {"id": reservation_id},
                {
                    "actual_json": canonical_json(merged),
                    "outstanding_json": canonical_json(new_hold),
                    "state": state,
                    "finished_at": now,
                },
            )
            if any(value > upper[key] for key, value in merged.items()):
                mark_budget_block(records, run, "budget_bound_exceeded", now)
            limits, used, _ = ledger_values(ledger)
            if is_exhausted(limits, used):
                mark_budget_block(records, run, "budget_exhausted", now)
            business_event(
                records,
                "budget_usage_recorded",
                run["task_id"],
                reservation_id=reservation_id,
                state=state,
                actual=merged,
                outstanding=new_hold,
            )
            return budget_view(ledger, warning_percent=self.warning_percent)

    def record_verified_progress(
        self,
        token: ExecutionToken,
        *,
        objective_key: str,
        fact_key: str,
        kind: str,
        artifact_id: str,
        now: float,
    ) -> str:
        """仅供宿主验证器写入事实；不接受模型自述或无产物百分比。"""
        if (
            not objective_key
            or not fact_key
            or kind
            not in {
                "coverage",
                "refutation",
                "validated_evidence",
                "verified_subproblem",
            }
        ):
            raise ValueError("必须提供有类别、目标及稳定事实标识的进展")
        with self.store.read() as records:
            artifact = dict(required(records, "artifacts", id=artifact_id))
            run = execution_record(records, token, now)
            snapshot_hash = content_digest(json.loads(run["snapshot_json"]))
        self._inspect_artifact(artifact, objective_key, kind, fact_key, snapshot_hash)
        with self.store.write() as records:
            run = execution_record(records, token, now)
            if (
                dict(required(records, "artifacts", id=artifact_id)) != artifact
                or artifact["run_id"] != run["id"]
            ):
                raise ConflictError("进展产物已变化或不属于此运行")
            identity = {
                "owner_task_id": run["budget_owner_task_id"],
                "objective_key": objective_key,
                "fact_key": fact_key,
                "kind": kind,
            }
            existing = records.one("budget_progress", **identity)
            if existing:
                return existing["id"]
            identifier = stable_id("budget-progress", *identity.values())
            records.add(
                "budget_progress",
                {"id": identifier, **identity, "run_id": run["id"], "artifact_id": artifact_id},
            )
            business_event(
                records,
                "budget_progress_recorded",
                run["task_id"],
                progress_id=identifier,
                artifact_id=artifact_id,
                objective_key=objective_key,
                progress_kind=kind,
            )
            return identifier

    def _inspect_artifact(self, artifact, objective, kind, fact, snapshot_hash):
        try:
            context = json.loads(artifact["context_json"])
        except (TypeError, ValueError) as error:
            raise ProgressEvidenceUnavailable("进展产物上下文无效") from error
        if (
            not isinstance(context, dict)
            or not artifact["available"]
            or context.get("producer") != "host_verifier"
            or context.get("objective_key") != objective
            or context.get("kind") != kind
            or context.get("fact_key") != fact
            or context.get("run_snapshot_sha256") != snapshot_hash
        ):
            raise ProgressEvidenceUnavailable("进展缺少与目标绑定的宿主验证事实")
        relative = Path(artifact["relative_path"])
        target = (self.artifacts_dir / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not target.is_relative_to(self.artifacts_dir)
        ):
            raise ProgressEvidenceUnavailable("进展产物引用越出归档")
        try:
            digest = sha256()
            with target.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != artifact["sha256"]:
                raise ProgressEvidenceUnavailable("进展产物内容与已登记摘要不符")
        except OSError as error:
            raise ProgressEvidenceUnavailable("进展产物缺失或不可读取") from error

    def _progress_snapshot(self, owner):
        with self.store.read() as records:
            rows = _progress_rows(records, owner)
        verified = []
        for item in rows:
            fact, artifact = item["fact"], item["artifact"]
            if not artifact["available"]:
                continue
            self._inspect_artifact(
                artifact,
                fact["objective_key"],
                fact["kind"],
                fact["fact_key"],
                item["run_snapshot_sha256"],
            )
            verified.append(
                {
                    key: fact[key]
                    for key in ("id", "objective_key", "fact_key", "kind", "artifact_id")
                }
            )
        return content_digest(rows), verified

    def _evidence_failure(self, task_id, owner, now):
        with self.store.write() as records:
            block_run = {"task_id": task_id, "budget_owner_task_id": owner}
            mark_budget_block(records, block_run, "progress_evidence_unavailable", now)

    def request_renewal(
        self,
        token: ExecutionToken,
        plan: RenewalPlan,
        *,
        now: float,
        suggested_increase: BudgetAmounts | None = None,
    ) -> dict:
        require_time(now)
        suggestion = suggested_increase.as_dict() if suggested_increase else None
        if suggestion is not None and not any(suggestion.values()):
            raise ValueError("追加建议必须为有限非零额度")
        request_hash = content_digest({"plan": plan.as_dict(), "suggestion": suggestion})
        with self.store.read() as records:
            replay = self._replay_request(records, token, request_hash)
            if replay is not None:
                return replay
            run = execution_record(records, token, now)
            owner, task_id = run["budget_owner_task_id"], run["task_id"]
            if required(records, "tasks", id=task_id)["mode"] == "diagnose":
                raise PermissionError("诊断授权有独立有限上限，不能通过续期扩大")
        try:
            stamp, progress = self._progress_snapshot(owner)
        except ProgressEvidenceUnavailable:
            self._evidence_failure(task_id, owner, now)
            raise
        if not progress and suggested_increase is not None:
            raise ValueError("没有经核验进展时只能申请用户配置，不能自动推荐追加")
        with self.store.write() as records:
            replay = self._replay_request(records, token, request_hash)
            if replay is not None:
                return replay
            run = execution_record(records, token, now)
            commands_clear(records, token.run_id)
            if any(
                item["state"] in {"reserved", "dispatched"}
                for item in records.find("budget_reservations", run_id=run["id"])
            ):
                raise PermissionError("必须先结束已发出步骤或撤销未启动的意图")
            ledger = meter_execution(records, run, now)
            basis = _basis(records, task_id, owner)
            if basis["progress_sha256"] != stamp:
                raise ConflictError("进展水位已变化，请重新生成申请")
            task = required(records, "tasks", id=task_id)
            snapshot = json.loads(run["snapshot_json"])
            owner_basis = {
                key: basis["owner"][key]
                for key in (
                    "task_id",
                    "input_revision",
                    "feedback_revision",
                    "acceptance_revision",
                    "configuration_sha256",
                )
            }
            if (
                task["input_revision"] != run["input_revision"]
                or task["feedback_revision"] != run["feedback_revision"]
                or required(records, "repositories", id=task["repository_id"])["admission_revision"]
                != run["admission_revision"]
                or task["acceptance_revision"] != snapshot.get("acceptance_revision")
                or json.loads(task["configuration_json"]) != snapshot.get("configuration")
                or basis["requester"]["target_revision"] != snapshot["target_revision"]
                or basis["requester"]["target_sha"] != snapshot["target_sha"]
                or owner_basis != snapshot.get("budget_owner")
            ):
                raise ConflictError("运行输入已变化，先安全交接并重新核对范围")
            basis_hash = content_digest(basis)
            existing = records.one("budget_renewals", owner_task_id=owner, basis_sha256=basis_hash)
            report = self._renewal_report(
                ledger,
                progress,
                plan.as_dict(),
                suggestion,
                content_digest(json.loads(run["snapshot_json"])),
            )
            recent_progress = report["recent_progress"]
            if existing:
                if json.loads(existing["report_json"]) != json.loads(canonical_json(report)):
                    raise ConflictError("同一续期依据已有不同申请，请刷新核对")
                return self._proposal_result(existing)
            for pending in records.find("budget_renewals", owner_task_id=owner, state="pending"):
                records.change("budget_renewals", {"id": pending["id"]}, {"state": "stale"})
            identifier = stable_id("budget-renewal", owner, basis_hash)
            records.add(
                "budget_renewals",
                {
                    "id": identifier,
                    "owner_task_id": owner,
                    "task_id": task_id,
                    "run_id": run["id"],
                    "basis_sha256": basis_hash,
                    "basis_json": canonical_json(basis),
                    "report_json": canonical_json(report),
                    "request_sha256": request_hash,
                    "state": "pending",
                },
            )
            view = budget_view(ledger)
            blocked = view["status"] in {"exhausted", "revoked"} or bool(
                records.find("block_reasons", task_id=task_id, resolved=0)
            )
            self._wait_flag(
                records,
                task_id,
                "budget_exhausted"
                if view["status"] == "exhausted"
                else "budget_renewal"
                if recent_progress
                else "budget_configuration",
            )
            business_event(
                records,
                "budget_renewal_requested",
                task_id,
                renewal_id=identifier,
                request_kind=report["kind"],
            )
            _finish(
                records,
                token,
                now,
                state="blocked" if blocked else "waiting",
                wait_reason=None
                if blocked
                else "budget_renewal"
                if recent_progress
                else "budget_configuration",
                outcome="budget_exhausted" if view["status"] == "exhausted" else None,
                use_transition=False,
            )
            return self._proposal_result(required(records, "budget_renewals", id=identifier))

    def _renewal_report(
        self, ledger, progress, plan, suggestion, snapshot_hash, *, revalidate=False
    ):
        approved = set(json.loads(ledger["approved_progress_json"]))
        recent = [fact for fact in progress if fact["id"] not in approved]
        if not recent and suggestion is not None:
            raise ValueError("没有尚未用于授权的新进展，不能重复推荐追加")
        return {
            "kind": "renewal" if recent else "configuration",
            "budget": budget_view(ledger, warning_percent=self.warning_percent),
            "progress": progress,
            "recent_progress": recent,
            "completed_goals": sorted({fact["objective_key"] for fact in progress}),
            "plan": plan,
            "suggested_increase": suggestion,
            "run_snapshot_sha256": snapshot_hash,
            "requires_revalidation": revalidate,
        }

    def refresh_renewal(
        self,
        operation_id: str,
        renewal_id: str,
        *,
        expected_revision: int,
        reason: str,
        now: float,
        plan: RenewalPlan | None = None,
        suggested_increase: BudgetAmounts | None = None,
        actor="local-user",
    ) -> dict:
        human(actor)
        require_time(now)
        if not reason.strip() or expected_revision <= 0:
            raise ValueError("刷新必须有用户依据及预期申请修订")
        suggestion = suggested_increase.as_dict() if suggested_increase else None
        if suggestion is not None and not any(suggestion.values()):
            raise ValueError("追加建议必须为有限非零额度")
        request = {
            "kind": "budget_refresh",
            "renewal": renewal_id,
            "revision": expected_revision,
            "plan": plan.as_dict() if plan else None,
            "suggestion": suggestion,
            "reason": reason,
            "actor": actor,
        }
        with self.store.read() as records:
            _, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            proposal = required(records, "budget_renewals", id=renewal_id)
            owner, task_id = proposal["owner_task_id"], proposal["task_id"]
        try:
            stamp, progress = self._progress_snapshot(owner)
        except ProgressEvidenceUnavailable:
            self._evidence_failure(task_id, owner, now)
            raise
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            proposal = required(records, "budget_renewals", id=renewal_id)
            if proposal["revision"] != expected_revision or proposal["state"] not in {
                "pending",
                "stale",
                "rejected",
                "scope_confirmation",
            }:
                raise ConflictError("申请已处理或修订变化，请刷新当前事项")
            if records.find("runs", budget_owner_task_id=owner, state="running"):
                raise PermissionError("预算归属仍有活跃运行，请先安全交接")
            task = required(records, "tasks", id=task_id)
            if task["user_excluded"] or task["cancel_requested"]:
                raise ConflictError("已排除或取消的任务须先明确重新启用")
            basis = _basis(records, task_id, owner)
            if stamp != basis["progress_sha256"]:
                raise ConflictError("核验期间进展水位变化，请重读")
            pending = records.find("budget_renewals", owner_task_id=owner, state="pending")
            if any(item["id"] != renewal_id for item in pending):
                raise ConflictError("此预算归属已有更新的待处理申请")
            previous_basis = json.loads(proposal["basis_json"])
            previous_report = json.loads(proposal["report_json"])
            revalidate = (
                previous_report["requires_revalidation"]
                or basis["requester"] != previous_basis["requester"]
                or basis["owner"] != previous_basis["owner"]
            )
            ledger = required(records, "budget_ledgers", owner_task_id=owner)
            selected_plan = plan.as_dict() if plan else previous_report["plan"]
            report = self._renewal_report(
                ledger,
                progress,
                selected_plan,
                suggestion,
                previous_report["run_snapshot_sha256"],
                revalidate=revalidate,
            )
            records.add(
                "budget_renewal_revisions",
                {
                    "renewal_id": renewal_id,
                    "revision": proposal["revision"],
                    "basis_json": proposal["basis_json"],
                    "report_json": proposal["report_json"],
                    "state": proposal["state"],
                },
            )
            revision = proposal["revision"] + 1
            records.change(
                "budget_renewals",
                {"id": renewal_id},
                {
                    "revision": revision,
                    "basis_sha256": content_digest(basis),
                    "basis_json": canonical_json(basis),
                    "report_json": canonical_json(report),
                    "request_sha256": content_digest(
                        {"plan": selected_plan, "suggestion": suggestion}
                    ),
                    "state": "pending",
                    "decision_json": None,
                    "result_json": None,
                },
            )
            if revalidate and task["state"] == "waiting":
                bump_task(records, task, wait_reason="resume_required")
            result = self._proposal_result(required(records, "budget_renewals", id=renewal_id))
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "budget_renewal_refreshed",
                task_id,
                operation_id=operation_id,
                renewal_id=renewal_id,
                revision=revision,
                actor=actor,
                reason=reason,
                requires_revalidation=revalidate,
            )
            return result

    def _replay_request(self, records, token, request_hash):
        existing = records.one("budget_renewals", run_id=token.run_id)
        if existing:
            run = required(records, "runs", id=token.run_id)
            if run["generation"] != token.generation:
                raise PermissionError("申请不属于当前代次")
            if existing["request_sha256"] != request_hash:
                raise ConflictError("同一运行已保存不同的续期申请")
            return self._proposal_result(existing)
        return None

    @staticmethod
    def _proposal_result(proposal):
        return {
            "id": proposal["id"],
            "revision": proposal["revision"],
            "state": proposal["state"],
            "owner_task_id": proposal["owner_task_id"],
            "task_id": proposal["task_id"],
            "run_id": proposal["run_id"],
            "report": json.loads(proposal["report_json"]),
        }

    def decide_renewal(
        self,
        operation_id: str,
        renewal_id: str,
        *,
        expected_revision: int,
        decision: str,
        reason: str,
        now: float,
        increase: BudgetAmounts | None = None,
        actor="local-user",
    ) -> dict:
        human(actor)
        require_time(now)
        if (
            not reason.strip()
            or expected_revision <= 0
            or decision
            not in {
                "approve",
                "reject",
                "scope_confirmation",
                "cancel",
            }
        ):
            raise ValueError("人工决定必须有有效类型、修订与依据")
        delta = increase.as_dict() if increase else None
        if decision == "approve" and (delta is None or not any(delta.values())):
            raise ValueError("批准必须给出有限非零追加额度")
        if decision != "approve" and delta is not None:
            raise ValueError("非批准操作不能追加额度")
        request = {
            "kind": "budget_decision",
            "renewal": renewal_id,
            "revision": expected_revision,
            "decision": decision,
            "increase": delta,
            "reason": reason,
            "actor": actor,
        }
        with self.store.read() as records:
            _, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            proposal = required(records, "budget_renewals", id=renewal_id)
            owner, task_id = proposal["owner_task_id"], proposal["task_id"]
        stamp = None
        if proposal["state"] == "pending":
            try:
                stamp, _ = self._progress_snapshot(owner)
            except ProgressEvidenceUnavailable:
                self._evidence_failure(task_id, owner, now)
                raise
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            proposal = required(records, "budget_renewals", id=renewal_id)
            stored_decision = {
                key: request[key] for key in ("decision", "increase", "reason", "actor")
            }
            if proposal["revision"] != expected_revision:
                raise ConflictError("申请修订已变化，请刷新")
            if proposal["state"] != "pending":
                if proposal["decision_json"] != canonical_json(stored_decision):
                    raise ConflictError("该申请已处理或失效，不能重复改额")
                result = json.loads(proposal["result_json"])
                remember_operation(records, operation_id, fingerprint, expected_revision, result)
                return result
            basis = _basis(records, task_id, owner)
            if (
                basis["progress_sha256"] != stamp
                or content_digest(basis) != proposal["basis_sha256"]
            ):
                raise ConflictError("账本、输入、代码、配置或进展水位已变化，请刷新申请")
            ledger = required(records, "budget_ledgers", owner_task_id=owner)
            task = required(records, "tasks", id=task_id)
            settle_ready(records, now)
            if decision == "approve":
                old_limits, _, _ = ledger_values(ledger)
                totals = plus(old_limits, delta)
                amounts(totals, complete=True, positive=True)
                changes = {
                    "limits_json": canonical_json(totals),
                    "authorization_revision": ledger["authorization_revision"] + 1,
                    "approved_progress_json": canonical_json(
                        sorted(
                            set(json.loads(ledger["approved_progress_json"]))
                            | {
                                fact["id"]
                                for fact in json.loads(proposal["report_json"])["progress"]
                            }
                        )
                    ),
                }
                if not old_limits:
                    changes["initial_limits_json"] = canonical_json(totals)
                ledger = save_ledger(records, ledger, **changes)
                if is_exhausted(*ledger_values(ledger)[:2]):
                    mark_budget_block(
                        records,
                        {"task_id": task_id, "budget_owner_task_id": owner},
                        "budget_exhausted",
                        now,
                    )
                    task = required(records, "tasks", id=task_id)
                safe = (
                    task["state"] == "waiting"
                    and task["wait_reason"] in {"budget_renewal", "budget_configuration"}
                    and not task["user_excluded"]
                    and not task["cancel_requested"]
                    and not ledger["revoked"]
                    and not records.find("block_reasons", resolved=0)
                    and not json.loads(proposal["report_json"])["requires_revalidation"]
                )
                if safe and is_ready(
                    records, {**dict(task), "state": "queued", "stop_requested": 0}
                ):
                    bump_task(
                        records, task, state="queued", wait_reason=None, budget_wait_reason=None
                    )
                elif task["state"] == "waiting" and task["wait_reason"] in {
                    "budget_renewal",
                    "budget_configuration",
                }:
                    bump_task(
                        records,
                        task,
                        wait_reason="resume_required",
                        budget_wait_reason="grant_pending_resume",
                    )
                elif task["state"] == "waiting" and task["wait_reason"] == "resume_required":
                    bump_task(records, task, budget_wait_reason="grant_pending_resume")
                state = "approved"
            elif decision == "scope_confirmation":
                if task["state"] != "blocked":
                    bump_task(records, task, state="waiting", wait_reason="approval")
                state = "scope_confirmation"
            elif decision == "cancel":
                cancel_dependencies(records, task, now)
                bump_task(
                    records,
                    task,
                    state="cancelled",
                    user_excluded=1,
                    cancel_requested=1,
                    wait_reason=None,
                    outcome="cancelled",
                )
                state = "cancelled"
            else:
                state = "rejected"
            updated = required(records, "tasks", id=task_id)
            result = {
                "renewal_id": renewal_id,
                "state": state,
                "budget": budget_view(ledger, warning_percent=self.warning_percent),
                "task_state": updated["state"],
                "wait_reason": updated["wait_reason"],
                "global_paused": bool(required(records, "global_control", singleton=1)["paused"]),
            }
            records.change(
                "budget_renewals",
                {"id": renewal_id},
                {
                    "state": state,
                    "decision_json": canonical_json(stored_decision),
                    "result_json": canonical_json(result),
                },
            )
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "budget_renewal_decided",
                task_id,
                operation_id=operation_id,
                renewal_id=renewal_id,
                decision=decision,
                actor=actor,
                reason=reason,
                increase=delta,
            )
            refresh_ready(records, now)
            return result


class BudgetedExecutor:
    """一个真实尝试对应一个预留；显式重试／子任务使用不同的稳定步骤键。"""

    def __init__(self, budgets: BudgetService, orchestrator, *, clock: Callable[[], float]):
        self.budgets, self.orchestrator, self.clock = budgets, orchestrator, clock

    def execute(
        self,
        token: ExecutionToken,
        operation_key: str,
        upper: BudgetAmounts,
        operation: Callable[[], Any],
        usage: Callable[[Any], dict[str, int]],
        *,
        purpose="model",
        preallocated_id=None,
    ):
        if preallocated_id is None:
            decision = self.budgets.reserve(
                token, operation_key, upper, purpose=purpose, now=self.clock()
            )
        else:
            with self.budgets.store.read() as records:
                row = required(records, "budget_reservations", id=preallocated_id)
                if (
                    row["run_id"] != token.run_id
                    or row["generation"] != token.generation
                    or row["operation_key"] != operation_key
                    or row["purpose"] != purpose
                    or json.loads(row["upper_json"]) != upper.as_dict()
                    or row["state"] != "reserved"
                ):
                    raise ConflictError("预先登记的步骤与当前执行不匹配")
            decision = BudgetDecision("reserved", preallocated_id)
        if not decision.allowed:
            raise BudgetDenied(decision)
        self.budgets.start(token, decision.reservation_id, now=self.clock())
        # 守卫失败仍保留已登记的发送意图，交给恢复核对，不能盲目重放。
        self.orchestrator.guard(
            token, now=self.clock(), purpose=purpose, reservation_id=decision.reservation_id
        )
        try:
            result = operation()
        except BaseException:
            actual = {"model_calls": 1} if purpose == "model" else {}
            self.budgets.settle(token, decision.reservation_id, actual, now=self.clock())
            raise
        try:
            actual = amounts(usage(result))
            if "active_ms" in actual or (purpose == "model" and actual.get("model_calls", 1) < 1):
                raise ValueError("用量回执包含无效计量")
        except Exception:
            actual = {"model_calls": 1} if purpose == "model" else {}
        self.budgets.settle(token, decision.reservation_id, actual, now=self.clock())
        return result
