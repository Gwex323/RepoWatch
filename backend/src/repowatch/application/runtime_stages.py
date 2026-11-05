"""图阶段的持久步骤／有效时长上限与业务—checkpoint 确认。"""

import json

from repowatch.application.budget_accounting import ledger_values, meter_execution
from repowatch.application.progress import ensure_window
from repowatch.application.stages import StageResults
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, execution_record, required
from repowatch.domain.runtime import RuntimePolicy
from repowatch.domain.task_records import canonical_json


class RuntimeStages:
    def __init__(self, store, artifacts):
        self.store, self.results = store, StageResults(store, artifacts)

    def begin(self, token, key, objective, *, now):
        identifier = self.results.begin(token, key, now=now)
        with self.store.write() as records:
            run = execution_record(records, token, now)
            existing = records.one("runtime_stages", id=identifier)
            if existing:
                if existing["objective_key"] != objective:
                    raise ConflictError("同一阶段不能改换目标")
                return dict(existing)
            policy = RuntimePolicy(
                **json.loads(run["snapshot_json"])["configuration"].get("runtime_policy", {})
            )
            ensure_window(records, run, objective)
            used = ledger_values(
                required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])
            )[1]
            records.add(
                "runtime_stages",
                {
                    "id": identifier,
                    "run_id": run["id"],
                    "objective_key": objective,
                    "policy_json": canonical_json(policy.as_dict()),
                    "started_active_ms": used["active_ms"],
                    "state": "running",
                },
            )
            records.change(
                "stage_intents", {"id": identifier}, {"thread_id": run["id"] + ":" + key}
            )
            return dict(required(records, "runtime_stages", id=identifier))

    def scope(self, stage_id, *, clock):
        return StageScope(self, stage_id, clock)

    def check(self, token, stage_id, *, now, consume=False):
        reason = None
        with self.store.write() as records:
            run = execution_record(records, token, now)
            stage = required(records, "runtime_stages", id=stage_id)
            if stage["run_id"] != run["id"] or stage["state"] != "running":
                raise PermissionError("图阶段不属于当前运行或已结束")
            ledger = meter_execution(records, run, now)
            used = ledger_values(ledger)[1]
            window = ensure_window(records, run, stage["objective_key"])
            policy = RuntimePolicy(**json.loads(stage["policy_json"]))
            max_steps, max_active = policy.max_steps, policy.max_active_ms
            if window["retry_authorization_id"] and not window["retry_satisfied"]:
                permit = required(
                    records, "retry_authorizations", id=window["retry_authorization_id"]
                )
                limits = json.loads(permit["limits_json"])
                max_steps = min(max_steps, limits["max_steps"])
                max_active = min(max_active, limits["max_active_ms"])
            if window["blocked_code"]:
                reason = window["blocked_code"]
            elif window["steps"] >= max_steps or stage["steps"] >= policy.max_steps:
                reason = "stage_step_limit"
            elif used["active_ms"] - window["started_active_ms"] >= max_active:
                reason = "stage_timeout"
            if reason:
                block(records, reason, run["task_id"], now=now, stage_id=stage_id)
            elif consume:
                records.change("runtime_stages", {"id": stage_id}, {"steps": stage["steps"] + 1})
                records.change(
                    "progress_windows",
                    {
                        "owner_task_id": run["budget_owner_task_id"],
                        "objective_key": stage["objective_key"],
                    },
                    {"steps": window["steps"] + 1},
                )
        if reason:
            raise PermissionError("图阶段触发持久安全上限或进展阻塞")

    def finish(self, token, stage_id, result, artifact_ids, *, checkpoint_id, now):
        self.results.save(token, stage_id, artifact_ids=artifact_ids, result=result, now=now)
        with self.store.write() as records:
            execution_record(records, token, now)
            records.change(
                "stage_intents",
                {"id": stage_id},
                {"state": "checkpoint_saved", "checkpoint_id": checkpoint_id},
            )
            records.change(
                "runtime_stages",
                {"id": stage_id},
                {
                    "state": "waiting"
                    if result["structured_result"]["status"] == "waiting"
                    else "completed"
                },
            )


class StageScope:
    def __init__(self, stages, stage_id, clock):
        self.stages, self.stage_id, self.clock = stages, stage_id, clock

    def preflight(self, token):
        self.stages.check(token, self.stage_id, now=self.clock())

    def touch(self, token):
        self.stages.check(token, self.stage_id, now=self.clock(), consume=True)
