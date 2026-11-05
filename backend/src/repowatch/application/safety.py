"""恢复前复核持久事实，不能把人工确认当作命令停止证明。"""

import json

from repowatch.application.budget_accounting import ledger_values
from repowatch.application.task_records import required
from repowatch.domain.budget import is_exhausted


def assert_quiescent(records, *, excluding_run=None, excluding_runs=(), allow_unknown_remote=False):
    excluded = set(excluding_runs) | {excluding_run}
    lease = required(records, "execution_lease", singleton=1)
    if lease["holder_run_id"] is not None and lease["holder_run_id"] not in excluded:
        raise PermissionError("仍有执行位持有者，需要先核对旧运行")
    if any(run["id"] not in excluded for run in records.find("runs", state="running")):
        raise PermissionError("存在未核对的遗留运行")
    for command in records.find("command_states"):
        if command["state"] not in {"stopped", "isolated"}:
            raise PermissionError("命令尚未核实停止或隔离")
        proof = json.loads(command["evidence_json"] or "{}")
        if not proof.get("stopped") or not proof.get("identity_verified"):
            raise PermissionError("缺少实际命令生命周期证明")
    if not allow_unknown_remote and any(
        operation["state"] in {"intent", "unknown"}
        for operation in records.find("publish_operations")
    ):
        raise PermissionError("远端操作结果未确认，不能重发或恢复")


def assert_task_budget(records, task):
    ledger = required(records, "budget_ledgers", owner_task_id=task["budget_owner_task_id"])
    limits, used, _ = ledger_values(ledger)
    if ledger["revoked"] or is_exhausted(limits, used):
        raise PermissionError("任务预算撤销或耗尽，先处理授权")
