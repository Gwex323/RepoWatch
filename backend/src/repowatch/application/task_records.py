"""稳定业务身份、输入快照与人工决定；不调用模型、命令或远端写接口。"""

import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from repowatch.adapters.windows_jobs import current_process
from repowatch.application.queue_state import refresh_ready, settle_ready
from repowatch.application.role_snapshots import resolve_run_roles
from repowatch.application.storage import BusinessStore, ConflictError, RecordPort
from repowatch.domain.task_records import (
    ExecutionToken,
    TriageDecision,
    TriageResult,
    canonical_json,
    content_digest,
    require_time,
    stable_id,
)
from repowatch.security import DEFAULT_REDACTOR


def required(records: RecordPort, collection: str, **identity) -> Mapping[str, Any]:
    row = records.one(collection, **identity)
    if row is None:
        raise LookupError("业务记录不存在")
    return row


def business_event(records: RecordPort, kind: str, task_id: str | None = None, **payload):
    records.add(
        "business_events",
        {
            "task_id": task_id,
            "kind": kind,
            "payload_json": canonical_json(DEFAULT_REDACTOR.clean(payload)),
        },
    )


def bump_task(records: RecordPort, task: Mapping[str, Any], **changes):
    changes["revision"] = task["revision"] + 1
    records.change("tasks", {"id": task["id"], "revision": task["revision"]}, changes)


def execution_record(records: RecordPort, token: ExecutionToken, now: float):
    require_time(now)
    lease = required(records, "execution_lease", singleton=1)
    run = required(records, "runs", id=token.run_id)
    if (
        lease["holder_run_id"] != (token.authority_run_id or token.run_id)
        or lease["generation"] != token.generation
        or lease["expires_at"] is None
        or lease["expires_at"] <= now
        or run["state"] != "running"
        or run["generation"] != token.generation
    ):
        raise PermissionError("执行权失效，必须先核对旧运行")
    if token.authority_run_id is not None:
        current, depth = run, 0
        while current["id"] != token.authority_run_id:
            parent = required(records, "runs", id=current["parent_run_id"])
            if parent["active_child_run_id"] != current["id"] or parent["state"] != "running":
                raise PermissionError("子任务委派已经失效")
            current, depth = parent, depth + 1
            if depth > 3:
                raise PermissionError("子任务超过最大委派深度")
    return run


def block(records: RecordPort, code: str, task_id: str | None, *, now: float, **details):
    settle_ready(records, now)
    identifier = stable_id("block", task_id or "global", code)
    if records.one("block_reasons", id=identifier) is None:
        records.add(
            "block_reasons",
            {
                "id": identifier,
                "task_id": task_id,
                "code": code,
                "details_json": canonical_json(details),
            },
        )
    else:
        row = required(records, "block_reasons", id=identifier)
        records.change(
            "block_reasons",
            {"id": identifier},
            {
                "details_json": canonical_json(details),
                "resolved": 0,
                "revision": row["revision"] + 1,
            },
        )
    control = required(records, "global_control", singleton=1)
    records.change(
        "global_control",
        {"singleton": 1},
        {
            "paused": 1,
            "revision": control["revision"] + 1,
        },
    )
    business_event(records, "blocked", task_id, reason_id=identifier, code=code)


def capture_run(records, task, run_id, generation, now, *, parent_run_id=None, snapshot=None):
    """仅由持有同一写事务的调度器调用；领取与快照原子提交。"""
    repository = (
        required(records, "repositories", id=task["repository_id"])
        if task["repository_id"] is not None
        else None
    )
    authorization = (
        required(records, "diagnostic_authorizations", task_id=task["id"])
        if repository is None
        else None
    )
    admission_revision = (
        repository["admission_revision"] if repository else authorization["policy_revision"]
    )
    owner = required(records, "tasks", id=task["budget_owner_task_id"])
    signals = records.find("pending_signals", task_id=task["id"], consumed_by_run_id=None)
    configuration = dict(snapshot or {})
    configuration.update(
        {
            "input": json.loads(task["input_json"]),
            "input_revision": task["input_revision"],
            "feedback_revision": task["feedback_revision"],
            "scope": "repository" if repository else "system_diagnostic",
            "repository_id": task["repository_id"],
            "admission_revision": admission_revision,
            "target_revision": repository["target_revision"] if repository else None,
            "target_sha": repository["target_sha"] if repository else None,
            "import_epoch": repository["import_epoch"] if repository else None,
            "scan_policy_revision": repository["scan_policy_revision"] if repository else None,
            "mode": task["mode"],
            "worker_process": current_process(),
            "configuration": json.loads(task["configuration_json"]),
            "roles": resolve_run_roles(records, json.loads(task["configuration_json"])),
            "acceptance_revision": task["acceptance_revision"],
            "budget_owner": {
                "task_id": owner["id"],
                "input_revision": owner["input_revision"],
                "feedback_revision": owner["feedback_revision"],
                "acceptance_revision": owner["acceptance_revision"],
                "configuration_sha256": content_digest(json.loads(owner["configuration_json"])),
            },
            "signals": [
                {
                    "id": item["id"],
                    "revision": item["revision"],
                    "kind": item["kind"],
                    "payload": json.loads(item["payload_json"]),
                }
                for item in signals
            ],
        }
    )
    records.add(
        "runs",
        {
            "id": run_id,
            "task_id": task["id"],
            "budget_owner_task_id": task["budget_owner_task_id"],
            "generation": generation,
            "parent_run_id": parent_run_id,
            "snapshot_json": canonical_json(configuration),
            "state": "running",
            "input_revision": task["input_revision"],
            "feedback_revision": task["feedback_revision"],
            "admission_revision": admission_revision,
            "graph_thread_id": run_id,
            "started_at": now,
            "budget_metered_ms": round(now * 1000),
        },
    )
    for signal in signals:
        records.add("run_signals", {"run_id": run_id, "signal_id": signal["id"]})


def create_task(
    records, repository_id, source_key, issue_id, mode, now, *, owner=None, input_payload=None
):
    if repository_id is None and (mode != "diagnose" or issue_id is not None or owner is not None):
        raise ValueError("无仓库任务仅允许具有独立预算的系统诊断")
    repository = required(records, "repositories", id=repository_id) if repository_id else None
    identifier = stable_id(
        "task",
        repository_id or "system-diagnostic",
        "issue:" + issue_id if issue_id else source_key,
    )
    needs_admission = issue_id is not None and repository["admission_mode"] != "all_bugs"
    input_json = canonical_json(input_payload or {})
    records.add(
        "tasks",
        {
            "id": identifier,
            "repository_id": repository_id,
            "source_key": source_key,
            "issue_id": issue_id,
            "mode": mode,
            "queued_at": now,
            "state": "waiting" if needs_admission else "queued",
            "wait_reason": "admission" if needs_admission else None,
            "input_json": input_json,
        },
    )
    records.change("tasks", {"id": identifier}, {"budget_owner_task_id": owner or identifier})
    if repository_id is not None:
        records.add(
            "task_sources",
            {"repository_id": repository_id, "source_key": source_key, "task_id": identifier},
        )
    if owner is None:
        records.add(
            "budget_ledgers",
            {
                "owner_task_id": identifier,
                "limits_json": "{}",
                "used_json": "{}",
                "reserved_json": "{}",
            },
        )
    records.add(
        "task_input_versions",
        {
            "task_id": identifier,
            "input_revision": 1,
            "feedback_revision": 1,
            "input_json": input_json,
        },
    )
    business_event(records, "task_created", identifier, source_key=source_key)
    return identifier


def cancel_dependencies(records, task, now):
    # Cancelling a non-owner waiter only detaches that waiter's links.
    records.remove("scan_dependencies", waiting_task_id=task["id"])
    if task["budget_owner_task_id"] == task["id"]:
        ledger = required(records, "budget_ledgers", owner_task_id=task["id"])
        records.change(
            "budget_ledgers",
            {"owner_task_id": task["id"]},
            {
                "revoked": 1,
                "revision": ledger["revision"] + 1,
            },
        )
        for dependent in records.find("tasks", budget_owner_task_id=task["id"]):
            if dependent["id"] != task["id"] and dependent["state"] not in {"ended", "cancelled"}:
                changes = {"stop_requested": 1}
                if dependent["state"] != "running":
                    changes.update(state="blocked", outcome="budget_revoked")
                bump_task(records, dependent, **changes)
                block(records, "budget_revoked", dependent["id"], now=now, owner_task_id=task["id"])
    elif records.find("scan_dependencies", scan_task_id=task["id"]):
        block(records, "shared_scan_cancelled", task["id"], now=now)


class TaskRecords:
    def __init__(self, store: BusinessStore):
        self.store = store

    def register_repository(self, github_id: str, *, admission_mode: str) -> str:
        if not github_id.strip() or admission_mode not in {"manual", "labels", "all_bugs"}:
            raise ValueError("仓库稳定身份和准入模式必须有效")
        with self.store.write() as records:
            existing = records.one("repositories", github_id=github_id)
            if existing:
                return existing["id"]
            identifier = stable_id("repository", github_id)
            records.add(
                "repositories",
                {
                    "id": identifier,
                    "github_id": github_id,
                    "admission_mode": admission_mode,
                },
            )
            business_event(records, "repository_registered", repository_id=identifier)
            return identifier

    def ensure_task(
        self,
        repository_id: str,
        source_key: str,
        *,
        issue_id: str | None = None,
        mode: str = "triage",
        now: float,
    ) -> str:
        require_time(now)
        if not source_key.strip() or (issue_id is not None and not issue_id.strip()):
            raise ValueError("任务来源与 Issue 稳定身份不能为空")
        if mode not in {
            "triage",
            "plan",
            "scan",
            "implement",
            "review",
            "amend",
            "distill",
            "maintenance",
            "diagnose",
        }:
            raise ValueError("未知任务模式")
        with self.store.write() as records:
            required(records, "repositories", id=repository_id)
            source = records.one("task_sources", repository_id=repository_id, source_key=source_key)
            existing = records.one("tasks", id=source["task_id"]) if source else None
            by_issue = (
                records.one("tasks", repository_id=repository_id, issue_id=issue_id)
                if issue_id
                else None
            )
            if existing and existing["issue_id"] != issue_id:
                raise ConflictError("来源已绑定另一 Issue，不能替换稳定身份")
            if existing and by_issue and existing["id"] != by_issue["id"]:
                raise ConflictError("来源与 Issue 指向不同主任务，必须先核对")
            if existing or by_issue:
                identifier = (existing or by_issue)["id"]
                if source is None:
                    records.add(
                        "task_sources",
                        {
                            "repository_id": repository_id,
                            "source_key": source_key,
                            "task_id": identifier,
                        },
                    )
                return identifier
            settle_ready(records, now)
            identifier = create_task(records, repository_id, source_key, issue_id, mode, now)
            refresh_ready(records, now)
            return identifier

    def observe(
        self,
        task_id: str,
        source_key: str,
        revision: str,
        payload: dict,
        *,
        kind: str = "input",
        now: float | None = None,
    ) -> str:
        now = time.time() if now is None else now
        if not source_key or not revision or kind not in {"input", "feedback"}:
            raise ValueError("信号必须有稳定来源、修订和类型")
        encoded = canonical_json(payload)
        with self.store.write() as records:
            task = required(records, "tasks", id=task_id)
            existing = records.one(
                "pending_signals", task_id=task_id, source_key=source_key, revision=revision
            )
            if existing:
                if existing["payload_json"] != encoded or existing["kind"] != kind:
                    raise ConflictError("同一来源修订出现不同内容，必须重新读取核对")
                return existing["id"]
            identifier = str(uuid4())
            settle_ready(records, now)
            records.add(
                "pending_signals",
                {
                    "id": identifier,
                    "task_id": task_id,
                    "source_key": source_key,
                    "revision": revision,
                    "payload_json": encoded,
                    "kind": kind,
                },
            )
            changes = {
                "input_revision": task["input_revision"] + (kind == "input"),
                "feedback_revision": task["feedback_revision"] + (kind == "feedback"),
            }
            if kind == "input":
                changes["input_json"] = encoded
            repository = required(records, "repositories", id=task["repository_id"])
            admitted = repository["admission_mode"] == "all_bugs" or task["selected"]
            reconsider = task["outcome"] == "policy_excluded" or task["wait_reason"] in {
                "information",
                "approval",
                "external_pr",
                "admission",
            }
            if reconsider and admitted and not task["user_excluded"]:
                changes.update(state="queued", mode="triage", wait_reason=None, outcome=None)
            bump_task(records, task, **changes)
            updated = required(records, "tasks", id=task_id)
            records.add(
                "task_input_versions",
                {
                    "task_id": task_id,
                    "input_revision": updated["input_revision"],
                    "feedback_revision": updated["feedback_revision"],
                    "input_json": updated["input_json"],
                },
            )
            business_event(
                records,
                "signal_observed",
                task_id,
                signal_id=identifier,
                input_revision=updated["input_revision"],
                feedback_revision=updated["feedback_revision"],
            )
            refresh_ready(records, now)
            return identifier

    def acknowledge(self, token: ExecutionToken, *, now: float) -> None:
        with self.store.write() as records:
            run = execution_record(records, token, now)
            for item in records.find("run_signals", run_id=token.run_id):
                signal = required(records, "pending_signals", id=item["signal_id"])
                if signal["task_id"] != run["task_id"]:
                    raise ConflictError("信号不属于当前主任务")
                if signal["consumed_by_run_id"] not in {None, token.run_id}:
                    raise ConflictError("信号已由其他运行确认")
                records.change(
                    "pending_signals",
                    {"id": signal["id"]},
                    {
                        "consumed_by_run_id": token.run_id,
                    },
                )
            business_event(records, "run_signals_acknowledged", run["task_id"], run_id=token.run_id)

    def save_triage(self, token: ExecutionToken, decision: TriageDecision, *, now: float) -> str:
        with self.store.write() as records:
            run = execution_record(records, token, now)
            if json.loads(run["snapshot_json"])["mode"] != "triage":
                raise PermissionError("只有分诊运行可提交分诊决定")
            task = required(records, "tasks", id=run["task_id"])
            repository = required(records, "repositories", id=task["repository_id"])
            identity = {
                "task_id": task["id"],
                "input_revision": run["input_revision"],
                "feedback_revision": run["feedback_revision"],
                "admission_revision": run["admission_revision"],
            }
            existing = records.one("triage_decisions", **identity)
            if existing:
                if (
                    existing["result"] != decision.result
                    or existing["reason_code"] != decision.reason_code
                    or existing["evidence_json"] != canonical_json(decision.evidence)
                ):
                    raise ConflictError("同一分诊输入的决定冲突，不能覆盖原记录")
                return existing["id"]
            identifier = str(uuid4())
            records.add(
                "triage_decisions",
                {
                    "id": identifier,
                    "run_id": token.run_id,
                    **identity,
                    "result": decision.result,
                    "reason_code": decision.reason_code,
                    "evidence_json": canonical_json(decision.evidence),
                },
            )
            current = (
                task["input_revision"] == run["input_revision"]
                and task["feedback_revision"] == run["feedback_revision"]
                and repository["admission_revision"] == run["admission_revision"]
            )
            if current and not task["user_excluded"] and not task["cancel_requested"]:
                state, wait, outcome = {
                    TriageResult.AUTO_REPAIR: ("queued", None, None),
                    TriageResult.NEEDS_INFORMATION: ("waiting", "information", None),
                    TriageResult.NEEDS_APPROVAL: ("waiting", "approval", None),
                    TriageResult.EXCLUDED: ("ended", None, "policy_excluded"),
                    TriageResult.EXTERNAL_REPAIR: ("waiting", "external_pr", None),
                    TriageResult.BLOCKED: ("blocked", None, "execution_blocked"),
                }[decision.result]
                transition = {
                    "state": state,
                    "wait_reason": wait,
                    "outcome": outcome,
                    "input_revision": run["input_revision"],
                    "feedback_revision": run["feedback_revision"],
                    "admission_revision": run["admission_revision"],
                    "mode": "implement"
                    if decision.result == TriageResult.AUTO_REPAIR
                    else "triage",
                }
                bump_task(records, task, pending_transition_json=canonical_json(transition))
                if decision.result == TriageResult.BLOCKED:
                    block(records, decision.reason_code, task["id"], now=now, triage_id=identifier)
            business_event(
                records, "triage_saved", task["id"], triage_id=identifier, current=current
            )
            return identifier

    def operate(
        self,
        operation_id: str,
        task_id: str,
        *,
        expected_revision: int,
        action: str,
        payload: dict | None = None,
        actor: str = "local-user",
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        if not operation_id or expected_revision <= 0 or not actor:
            raise ValueError("人工操作必须有稳定标识、预期修订及操作者")
        payload = payload or {}
        request_hash = content_digest(
            {
                "task": task_id,
                "revision": expected_revision,
                "action": action,
                "payload": payload,
                "actor": actor,
            }
        )
        with self.store.write() as records:
            existing = records.one("user_operations", id=operation_id)
            if existing:
                if existing["request_sha256"] != request_hash:
                    raise ConflictError("同一操作标识的请求内容不同")
                return json.loads(existing["result_json"])
            task = required(records, "tasks", id=task_id)
            if task["revision"] != expected_revision:
                raise ConflictError("对象修订已变化，请刷新核对")
            if task["mode"] == "diagnose" and action not in {"cancel", "exclude"}:
                raise PermissionError("诊断不转为业务任务；重新测试需要新的明确诊断授权")
            settle_ready(records, now)
            changes: dict[str, Any] = {}
            running = task["state"] == "running"
            if action in {"exclude", "cancel"}:
                cancel_dependencies(records, task, now)
                changes.update(
                    user_excluded=1,
                    stop_requested=int(running),
                    outcome="user_excluded" if action == "exclude" else "cancelled",
                )
                if action == "cancel":
                    changes["cancel_requested"] = 1
                if not running:
                    changes.update(
                        state="ended" if action == "exclude" else "cancelled", wait_reason=None
                    )
            elif action == "reenable":
                if running:
                    raise ConflictError("旧运行尚未停止，不能重新启用")
                binding = records.one("managed_pull_requests", task_id=task_id)
                terminal_delivery = binding and binding["state"] in {"closed", "merged"}
                if task["budget_owner_task_id"] == task_id:
                    ledger = required(records, "budget_ledgers", owner_task_id=task_id)
                    records.change(
                        "budget_ledgers",
                        {"owner_task_id": task_id},
                        {
                            "revoked": 0,
                            "revision": ledger["revision"] + 1,
                        },
                    )
                changes.update(
                    user_excluded=0,
                    cancel_requested=0,
                    stop_requested=0,
                    selected=1,
                    state="waiting" if terminal_delivery else "queued",
                    mode="scan" if task["mode"] == "scan" else "triage",
                    wait_reason="delivery_review" if terminal_delivery else None,
                    outcome=None,
                )
            elif action in {"select", "request_triage", "supply_information"}:
                if task["user_excluded"] or task["cancel_requested"]:
                    raise ConflictError("人工排除或取消必须明确重新启用")
                if task["state"] == "ended" and task["outcome"] != "policy_excluded":
                    raise ConflictError("交付终态不能由普通选择重开")
                changes["selected"] = 1
                if action == "supply_information":
                    if not payload:
                        raise ValueError("补充材料不能为空")
                    context = json.loads(task["input_json"])
                    context["human_context"] = payload
                    changes.update(
                        input_revision=task["input_revision"] + 1,
                        input_json=canonical_json(context),
                    )
                if task["state"] not in {"running", "blocked"}:
                    changes.update(
                        state="queued",
                        mode="scan" if task["mode"] == "scan" else "triage",
                        wait_reason=None,
                        outcome=None,
                    )
            elif action == "update_configuration":
                if not payload or DEFAULT_REDACTOR.clean(payload) != payload:
                    raise ValueError("任务配置必须有效且不能包含凭据")
                resolve_run_roles(records, payload)
                changes["configuration_json"] = canonical_json(payload)
                if task["mode"] == "scan":
                    repository = required(records, "repositories", id=task["repository_id"])
                    records.change(
                        "repositories",
                        {"id": repository["id"]},
                        {
                            "scan_ready_sha": None,
                            "scan_ready_json": None,
                            "revision": repository["revision"] + 1,
                        },
                    )
                if running:
                    changes["stop_requested"] = 1
            elif action == "set_priority":
                manual, severity = payload.get("manual"), payload.get("severity", task["severity"])
                if (
                    manual is not None
                    and (type(manual) is not int or not -1000 <= manual <= 1000)
                    or type(severity) is not int
                    or not 0 <= severity <= 4
                ):
                    raise ValueError("优先级无效")
                changes.update(manual_priority=manual, severity=severity)
            else:
                raise ValueError("不支持的人工任务操作")
            bump_task(records, task, **changes)
            updated = required(records, "tasks", id=task_id)
            if updated["input_revision"] != task["input_revision"]:
                records.add(
                    "task_input_versions",
                    {
                        "task_id": task_id,
                        "input_revision": updated["input_revision"],
                        "feedback_revision": updated["feedback_revision"],
                        "input_json": updated["input_json"],
                    },
                )
            result = {
                "task_id": task_id,
                "revision": updated["revision"],
                "state": updated["state"],
            }
            records.add(
                "user_operations",
                {
                    "id": operation_id,
                    "request_sha256": request_hash,
                    "expected_revision": expected_revision,
                    "result_json": canonical_json(result),
                },
            )
            business_event(
                records,
                "user_operation",
                task_id,
                operation_id=operation_id,
                action=action,
                actor=actor,
                revision=updated["revision"],
            )
            refresh_ready(records, now)
            return result

    def bind_managed_pr(self, task_id: str, pr_repository_id: str, pr_id: str, *, state="open"):
        if not pr_repository_id or not pr_id or state not in {"open", "closed", "merged"}:
            raise ValueError("PR 稳定身份和状态无效")
        with self.store.write() as records:
            task = required(records, "tasks", id=task_id)
            if task["issue_id"] is None:
                raise ConflictError("没有已确认 Issue 身份，不能绑定受管 PR")
            existing = records.one("managed_pull_requests", task_id=task_id)
            identity = {
                "task_id": task_id,
                "issue_repository_id": task["repository_id"],
                "issue_id": task["issue_id"],
                "pr_repository_id": pr_repository_id,
                "pr_id": pr_id,
            }
            if existing:
                if any(existing[key] != value for key, value in identity.items()):
                    raise ConflictError("受管 PR 生命周期绑定不能替换")
                records.change("managed_pull_requests", {"task_id": task_id}, {"state": state})
            else:
                records.add("managed_pull_requests", {**identity, "state": state})
            business_event(records, "managed_pr_observed", task_id, pr_id=pr_id, state=state)

    def observe_external_pr(self, repository_id: str, issue_id: str, pr_id: str, payload: dict):
        with self.store.write() as records:
            required(records, "repositories", id=repository_id)
            identity = {"repository_id": repository_id, "issue_id": issue_id, "pr_id": pr_id}
            values = {"payload_json": canonical_json(payload)}
            if records.one("external_pr_observations", **identity):
                records.change("external_pr_observations", identity, values)
            else:
                records.add("external_pr_observations", {**identity, **values})
