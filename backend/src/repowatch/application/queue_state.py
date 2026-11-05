"""队列老化的持久计时；所有就绪门槛变更前结算、变更后重算。"""

from repowatch.application.storage import RecordPort
from repowatch.domain.task_records import require_time


def is_ready(records: RecordPort, task) -> bool:
    if task["mode"] == "diagnose":
        return False
    control = records.one("global_control", singleton=1)
    repository = records.one("repositories", id=task["repository_id"])
    budget = records.one("budget_ledgers", owner_task_id=task["budget_owner_task_id"])
    if (
        control["paused"]
        or repository["paused"]
        or task["state"] != "queued"
        or task["user_excluded"]
        or task["cancel_requested"]
        or task["stop_requested"]
        or budget is None
        or budget["revoked"]
        or records.find("scan_dependencies", waiting_task_id=task["id"])
    ):
        return False
    if task["mode"] in {"implement", "amend"}:
        return (
            task["issue_id"] is not None
            and bool(repository["target_sha"])
            and repository["target_sha"] == repository["scan_ready_sha"]
        )
    return task["mode"] != "diagnose"


def settle_ready(records: RecordPort, now: float):
    require_time(now)
    for task in records.find("tasks"):
        if task["ready_since"] is not None:
            records.change(
                "tasks",
                {"id": task["id"]},
                {
                    "ready_seconds": task["ready_seconds"] + max(0, now - task["ready_since"]),
                    "ready_since": None,
                },
            )


def refresh_ready(records: RecordPort, now: float):
    require_time(now)
    for task in records.find("tasks"):
        if task["ready_since"] is None and is_ready(records, task):
            records.change("tasks", {"id": task["id"]}, {"ready_since": now})
