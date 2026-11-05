"""控制面只读投影；读取或刷新不领取任务、不发起模型。"""

import json

from repowatch.application.artifacts import EventReader
from repowatch.application.task_records import required
from repowatch.security import DEFAULT_REDACTOR


def public_record(row):
    return DEFAULT_REDACTOR.clean(
        {
            key.removesuffix("_json"): json.loads(value) if value is not None else None
            for key, value in row.items()
            if key.endswith("_json")
        }
        | {key: value for key, value in row.items() if not key.endswith("_json")}
    )


class Console:
    def __init__(self, store, budgets, providers, roles):
        self.store, self.budgets, self.providers, self.roles = store, budgets, providers, roles
        self.events = EventReader(store)

    def state(self):
        with self.store.read() as records:
            tasks = [public_record(row) for row in records.find("tasks")]
            result = {
                "control": public_record(required(records, "global_control", singleton=1)),
                "repositories": [public_record(row) for row in records.find("repositories")],
                "tasks": tasks,
                "reasons": [
                    public_record(row) for row in records.find("block_reasons", resolved=0)
                ],
                "renewals": [public_record(row) for row in records.find("budget_renewals")],
                "capabilities": [public_record(row) for row in records.find("capability_checks")],
                "diagnostics": [
                    public_record(row) for row in records.find("diagnostic_authorizations")
                ],
            }
        for task in tasks:
            task["budget"] = self.budgets.view(task["budget_owner_task_id"])
        result["providers"], result["roles"] = self.providers.list(), self.roles.list()
        return result

    def task(self, identifier):
        with self.store.read() as records:
            task = public_record(required(records, "tasks", id=identifier))
            task["runs"] = [public_record(row) for row in records.find("runs", task_id=identifier)]
            task["reasons"] = [
                public_record(row) for row in records.find("block_reasons", task_id=identifier)
            ]
            task["progress"] = [
                public_record(row)
                for row in records.find(
                    "budget_progress", owner_task_id=task["budget_owner_task_id"]
                )
            ]
            task["attempts"] = [
                public_record(row) for row in records.find("progress_attempts", task_id=identifier)
            ]
            task["progress_windows"] = [
                public_record(row)
                for row in records.find(
                    "progress_windows", owner_task_id=task["budget_owner_task_id"]
                )
            ]
        task["budget"] = self.budgets.view(task["budget_owner_task_id"])
        return task
