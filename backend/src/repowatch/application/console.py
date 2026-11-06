"""控制面只读投影；读取或刷新不领取任务、不发起模型。"""

import json

from repowatch.application.acceptance import acceptance_view
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
            task["acceptance"] = DEFAULT_REDACTOR.clean(acceptance_view(records, task))
            task["triage_decisions"] = [
                public_record(row) for row in records.find("triage_decisions", task_id=identifier)
            ]
            contexts = records.find("issue_contexts", task_id=identifier)
            latest = max(contexts, key=lambda item: item["created_at"], default=None)
            task["issue_context"] = public_record(latest) if latest else None
            task["publication_intents"] = [
                public_record(row)
                for row in records.find("publication_intents", task_id=identifier)
            ]
            task["repair_sessions"] = [
                public_record(row) for row in records.find("repair_sessions", task_id=identifier)
            ]
            task["relation_decisions"] = [
                public_record(row) for row in records.find("relation_decisions", task_id=identifier)
            ]
            task["candidate_issue_decisions"] = [
                public_record(row)
                for row in records.find("candidate_issue_decisions", task_id=identifier)
            ]
            task["distillation_intents"] = [
                public_record(row)
                for row in records.find("distillation_intents", task_id=identifier)
            ]
            task["runs"] = [public_record(row) for row in records.find("runs", task_id=identifier)]
            task["artifacts"] = [
                {
                    "id": artifact["id"],
                    "sha256": artifact["sha256"],
                    "available": artifact["available"],
                    "context": DEFAULT_REDACTOR.clean(json.loads(artifact["context_json"])),
                }
                for run in task["runs"]
                for artifact in records.find("artifacts", run_id=run["id"])
            ]
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

    def artifact(self, task_id, artifact_id, artifacts, *, now):
        with self.store.read() as records:
            required(records, "tasks", id=task_id)
            artifact = required(records, "artifacts", id=artifact_id)
            run = required(records, "runs", id=artifact["run_id"])
            if run["task_id"] != task_id:
                raise PermissionError("产物不属于当前任务")
        artifact = artifacts.require(artifact_id, now=now, block_on_failure=False)
        with (artifacts.root / artifact["relative_path"]).open("rb") as source:
            content = source.read(1024 * 1024 + 1)
        return {
            "artifact_id": artifact_id,
            "sha256": artifact["sha256"],
            "truncated": len(content) > 1024 * 1024,
            "content": DEFAULT_REDACTOR.clean(content[: 1024 * 1024].decode("utf-8", "replace")),
        }
