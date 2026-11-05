"""基于实际核验产物的持久无进展／振荡窗口；运行或 SHA 不重置历史。"""

import json

from repowatch.application.budget_accounting import meter_execution
from repowatch.application.budgets import ProgressEvidenceUnavailable
from repowatch.application.task_records import block, business_event, execution_record, required
from repowatch.domain.runtime import RuntimePolicy, fingerprint
from repowatch.domain.task_records import canonical_json, stable_id


def ensure_window(records, run, objective):
    owner = run["budget_owner_task_id"]
    identity = {"owner_task_id": owner, "objective_key": objective}
    current = records.one("progress_windows", **identity)
    used = json.loads(required(records, "budget_ledgers", owner_task_id=owner)["used_json"])
    if current is None:
        records.add("progress_windows", {**identity, "started_active_ms": used.get("active_ms", 0)})
        current = required(records, "progress_windows", **identity)
    permit = json.loads(run["snapshot_json"]).get("retry_authorization")
    if permit and permit["id"] != current["retry_authorization_id"]:
        authorization = required(records, "retry_authorizations", id=permit["id"])
        authority = run
        while authority["parent_run_id"] is not None:
            authority = required(records, "runs", id=authority["parent_run_id"])
        if authorization["consumed_by_run_id"] != authority["id"]:
            raise PermissionError("重试授权不属于当前运行")
        records.change(
            "progress_windows",
            identity,
            {
                "window": current["window"] + 1,
                "attempts": 0,
                "consecutive_empty": 0,
                "steps": 0,
                "blocked_code": None,
                "retry_authorization_id": permit["id"],
                "retry_satisfied": 0,
                "started_active_ms": used.get("active_ms", 0),
            },
        )
    return required(records, "progress_windows", **identity)


class ProgressService:
    def __init__(self, store, artifacts, budgets):
        self.store, self.artifacts, self.budgets = store, artifacts, budgets

    def record(self, token, objective, artifact_id, *, now, policy=None):
        artifact = self.artifacts.require(artifact_id, run_id=token.run_id, now=now)
        path = self.artifacts.root / artifact["relative_path"]
        if path.stat().st_size > 1000000:
            raise ValueError("阶段比较产物过大")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        context = json.loads(artifact["context_json"])
        if (
            not isinstance(evidence, dict)
            or context.get("producer") != "host_stage_verifier"
            or context.get("objective_key") != objective
        ):
            raise PermissionError("阶段比较产物缺少宿主核验归属")
        if (
            evidence.get("producer") != "host_stage_verifier"
            or evidence.get("objective_key") != objective
        ):
            raise PermissionError("只有与目标关联的宿主实际核验记录可推进窗口")
        if evidence.get("status") in {"running", "waiting"}:
            return {"recorded": False, "reason": "normal_wait"}
        if evidence.get("status") not in {"completed", "timed_out"}:
            raise ValueError("只记录已结束的可比尝试")
        with self.store.read() as records:
            run = execution_record(records, token, now)
            owner = run["budget_owner_task_id"]
            pinned = RuntimePolicy(
                **json.loads(run["snapshot_json"])["configuration"].get("runtime_policy", {})
            )
        if policy is not None and policy != pinned:
            raise PermissionError("运行中不能改写检测策略")
        policy = pinned
        try:
            _, verified = self.budgets._progress_snapshot(owner)
        except ProgressEvidenceUnavailable:
            with self.store.write() as records:
                block(records, "progress_evidence_unavailable", run["task_id"], now=now)
            raise
        valid_ids = {row["id"] for row in verified if row["objective_key"] == objective}
        claimed = set(evidence.get("verified_fact_ids", []))
        if not claimed <= valid_ids:
            raise PermissionError("不能用模型自述或无效证据作为进展")
        digest = fingerprint(objective, evidence.get("comparison", {}))
        identifier = stable_id("progress-attempt", token.run_id, artifact_id)
        with self.store.write() as records:
            run = execution_record(records, token, now)
            meter_execution(records, run, now)
            current = ensure_window(records, run, objective)
            old = records.one("progress_attempts", id=identifier)
            if old:
                return {"recorded": True, "attempt_id": identifier, "window": dict(current)}
            new = sorted(
                item
                for item in claimed
                if records.one("progress_consumptions", progress_id=item) is None
            )
            current_snapshot = json.loads(run["snapshot_json"])
            for item in new:
                fact = required(records, "budget_progress", id=item)
                source = required(records, "runs", id=fact["run_id"])
                source_snapshot = json.loads(source["snapshot_json"])
                if source_snapshot.get("target_sha") != current_snapshot.get(
                    "target_sha"
                ) or source_snapshot["configuration"].get("code_version") != current_snapshot[
                    "configuration"
                ].get("code_version"):
                    raise PermissionError("旧版本证据须重新核验，不能直接记为新进展")
            records.add(
                "progress_attempts",
                {
                    "id": identifier,
                    "task_id": run["task_id"],
                    "run_id": run["id"],
                    "objective_key": objective,
                    "fingerprint": digest,
                    "evidence_json": canonical_json(
                        {
                            "artifact_id": artifact_id,
                            "comparison": evidence.get("comparison", {}),
                            "progress_ids": new,
                        }
                    ),
                    "window": current["window"],
                    "recorded_at": now,
                    "outcome": evidence["status"],
                    "ordinal": current["attempts"] + 1,
                },
            )
            for item in new:
                records.add(
                    "progress_consumptions",
                    {
                        "progress_id": item,
                        "owner_task_id": owner,
                        "objective_key": objective,
                        "attempt_id": identifier,
                    },
                )
            empty = 0 if new else current["consecutive_empty"] + 1
            reason = None
            attempts = []
            for task in records.find("tasks", budget_owner_task_id=owner):
                attempts.extend(
                    row
                    for row in records.find(
                        "progress_attempts", task_id=task["id"], objective_key=objective
                    )
                    if row["window"] == current["window"]
                )
            attempts.sort(key=lambda row: row["ordinal"])
            signatures = [row["fingerprint"] for row in attempts]
            length = policy.oscillation_cycles * 2 + 1
            repeated = signatures[-length:]
            oscillating = (
                len(repeated) == length
                and len(set(repeated)) == 2
                and all(repeated[index] == repeated[index % 2] for index in range(length))
                and all(
                    not json.loads(row["evidence_json"]).get("progress_ids")
                    for row in attempts[-length:]
                )
            )
            if evidence["status"] == "timed_out":
                reason = "stage_timeout"
            elif not new and oscillating:
                reason = "oscillation"
            elif empty >= policy.no_progress_attempts:
                reason = "no_progress"
            if current["retry_authorization_id"] and not current["retry_satisfied"] and not new:
                permit = required(
                    records, "retry_authorizations", id=current["retry_authorization_id"]
                )
                if current["attempts"] + 1 >= json.loads(permit["limits_json"])["max_attempts"]:
                    reason = reason or "bounded_retry_exhausted"
            changes = {
                "attempts": current["attempts"] + 1,
                "consecutive_empty": empty,
                "progress_revision": current["progress_revision"] + len(new),
                "blocked_code": reason or current["blocked_code"],
                "retry_satisfied": int(bool(new) or current["retry_satisfied"]),
            }
            records.change(
                "progress_windows", {"owner_task_id": owner, "objective_key": objective}, changes
            )
            if reason:
                block(
                    records,
                    reason,
                    run["task_id"],
                    now=now,
                    window=current["window"],
                    attempt_ids=[row["id"] for row in attempts],
                    fingerprints=signatures[-length:],
                )
            business_event(
                records,
                "progress_attempt_completed",
                run["task_id"],
                attempt_id=identifier,
                progress_ids=new,
                blocked=reason,
            )
            return {
                "recorded": True,
                "attempt_id": identifier,
                "progress": bool(new),
                "blocked": reason,
            }
