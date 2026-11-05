"""正式单执行位 Worker；诊断可独立排队，仓库执行必须有受信任适配。"""

import json

from repowatch.application.budgets import ProgressEvidenceUnavailable
from repowatch.application.storage import StorageError
from repowatch.application.task_records import block, required
from repowatch.domain.budget import BudgetDenied, RenewalPlan
from repowatch.runtime.engine import FormalRuntime
from repowatch.worker.diagnostics import DiagnosticRunner
from repowatch.worker.heartbeat import ExecutionHeartbeat
from repowatch.worker.role_probe import RoleProbe


class Worker:
    def __init__(self, services, *, plan_factory=None):
        self.services, self.plan_factory = services, plan_factory
        self.runtime = FormalRuntime(services)

    def run_once(self):
        services = self.services
        services.diagnostics.expire_queued(now=services.clock())
        with services.store.read() as records:
            queued = [
                row
                for row in records.find("diagnostic_authorizations", state="queued")
                if row["deadline"] > services.clock()
                and required(records, "tasks", id=row["task_id"])["state"] == "queued"
            ]
        if queued:
            try:
                token = services.diagnostics.claim(queued[0]["task_id"], now=services.clock())
            except PermissionError:
                return {"state": "waiting", "reason": "diagnostic_safety_preconditions"}
            with ExecutionHeartbeat(services.queue, token, services.clock) as heartbeat:
                try:
                    if queued[0]["kind"] == "provider_capability":
                        result = RoleProbe(
                            services.diagnostics,
                            services.adapter,
                            services.budgets,
                            services.queue,
                            services.artifacts,
                            services.roles,
                            clock=services.clock,
                        ).run(token)
                    else:
                        result = DiagnosticRunner(
                            services.diagnostics,
                            services.adapter,
                            services.budgets,
                            services.queue,
                            clock=services.clock,
                        ).run(token)
                    heartbeat.check()
                    return {"state": "diagnostic_completed", "result": result}
                except BudgetDenied:
                    self.runtime._cancel_unstarted(token)
                    result = services.diagnostics.finish(
                        token, now=services.clock(), passed=False, checks={}
                    )
                    return {"state": "diagnostic_failed", "result": result}
                except StorageError:
                    return {"state": "reconciliation_required", "run_id": token.run_id}
                except Exception:
                    self.runtime._cancel_unstarted(token)
                    try:
                        result = services.diagnostics.finish(
                            token, now=services.clock(), passed=False, checks={}
                        )
                    except PermissionError:
                        return {"state": "reconciliation_required", "run_id": token.run_id}
                    return {"state": "diagnostic_failed", "result": result}
        token = services.queue.claim(now=services.clock())
        if token is None:
            return {"state": "idle"}
        if self.plan_factory is None:
            with services.store.write() as records:
                run = required(records, "runs", id=token.run_id)
                block(records, "workspace_adapter_missing", run["task_id"], now=services.clock())
            services.queue.complete(
                token, now=services.clock(), state="blocked", outcome="workspace_adapter_missing"
            )
            return {"state": "blocked", "reason": "workspace_adapter_missing"}
        with ExecutionHeartbeat(services.queue, token, services.clock) as heartbeat:
            try:
                plan = self.plan_factory(token)
                result = self.runtime.run(token, plan)
                heartbeat.check()
                with services.store.read() as records:
                    run = required(records, "runs", id=token.run_id)
                    reasons = records.find("block_reasons", resolved=0)
                    stage = records.one("stage_intents", run_id=token.run_id, stage_key=plan.key)
                    evidence_ids = json.loads(stage["result_json"])["artifact_ids"]
                goal_completed = False
                for artifact_id in evidence_ids:
                    artifact = services.artifacts.require(
                        artifact_id, run_id=token.run_id, now=services.clock()
                    )
                    if (
                        json.loads(artifact["context_json"]).get("producer")
                        == "host_stage_verifier"
                    ):
                        evidence = json.loads(
                            (services.artifacts.root / artifact["relative_path"]).read_text(
                                encoding="utf-8"
                            )
                        )
                        goal_completed = evidence.get("goal_completed") is True
                if reasons:
                    services.queue.complete(
                        token, now=services.clock(), state="blocked", outcome=reasons[0]["code"]
                    )
                    return {"state": "blocked", "reason": reasons[0]["code"]}
                state = (
                    "waiting"
                    if result.status == "waiting"
                    else "ended"
                    if goal_completed
                    else "queued"
                )
                services.queue.complete(
                    token,
                    now=services.clock(),
                    state=state,
                    wait_reason="human_information" if state == "waiting" else None,
                )
                with services.store.read() as records:
                    actual = required(records, "tasks", id=run["task_id"])["state"]
                return {"state": actual, "run_id": token.run_id}
            except BudgetDenied:
                try:
                    services.budgets.request_renewal(
                        token,
                        RenewalPlan(
                            ("完成原目标的剩余核验",), "按原范围继续", "取得下一项有效证据"
                        ),
                        now=services.clock(),
                    )
                except ProgressEvidenceUnavailable:
                    services.queue.complete(
                        token,
                        now=services.clock(),
                        state="blocked",
                        outcome="progress_evidence_unavailable",
                    )
                return {"state": "budget_wait", "run_id": token.run_id}
            except StorageError:
                return {"state": "reconciliation_required", "run_id": token.run_id}
            except Exception:
                with services.store.write() as records:
                    run = required(records, "runs", id=token.run_id)
                    block(
                        records, "runtime_execution_rejected", run["task_id"], now=services.clock()
                    )
                try:
                    services.queue.complete(
                        token,
                        now=services.clock(),
                        state="blocked",
                        outcome="runtime_execution_rejected",
                    )
                except PermissionError:
                    return {"state": "reconciliation_required", "run_id": token.run_id}
                return {"state": "blocked", "reason": "runtime_execution_rejected"}
