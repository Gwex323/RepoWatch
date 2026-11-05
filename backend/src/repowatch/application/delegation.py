"""独立子运行共享父执行权与账本，委派期间父运行禁止新执行。"""

import json

from repowatch.application.storage import ConflictError
from repowatch.application.task_records import business_event, execution_record
from repowatch.domain.task_records import ExecutionToken, canonical_json


class Delegation:
    def __init__(self, store):
        self.store = store

    def start(self, parent_token, child_id, *, mode, role_key, now):
        with self.store.write() as records:
            parent = execution_record(records, parent_token, now)
            if parent["active_child_run_id"] is not None:
                raise PermissionError("已有同步子任务，不能并行委派")
            if records.one("runs", id=child_id):
                raise ConflictError("子运行标识已使用，先核对已有结果")
            snapshot = json.loads(parent["snapshot_json"])
            if role_key not in snapshot.get("roles", {}):
                raise PermissionError("子任务模型未预先绑定")
            snapshot["mode"], snapshot["delegated_role_key"] = mode, role_key
            records.add(
                "runs",
                {
                    "id": child_id,
                    "task_id": parent["task_id"],
                    "budget_owner_task_id": parent["budget_owner_task_id"],
                    "generation": parent_token.generation,
                    "parent_run_id": parent["id"],
                    "snapshot_json": canonical_json(snapshot),
                    "state": "running",
                    "input_revision": parent["input_revision"],
                    "feedback_revision": parent["feedback_revision"],
                    "admission_revision": parent["admission_revision"],
                    "graph_thread_id": child_id,
                    "started_at": now,
                    "budget_metered_ms": round(now * 1000),
                },
            )
            records.change("runs", {"id": parent["id"]}, {"active_child_run_id": child_id})
            token = ExecutionToken(
                child_id, parent_token.generation, parent_token.authority_run_id or parent["id"]
            )
            execution_record(records, token, now)
            business_event(
                records,
                "child_run_started",
                parent["task_id"],
                run_id=child_id,
                parent_run_id=parent["id"],
            )
            return token
