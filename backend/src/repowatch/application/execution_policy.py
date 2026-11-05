"""普通运行与明确诊断共用的发起门槛；诊断豁免不延伸至写入。"""

from repowatch.application.task_records import required

DIAGNOSTIC_PURPOSE = {
    "storage_probe": "tool",
    "provider_connection": "model",
    "provider_capability": "model",
}
DIAGNOSTIC_POLICY_REVISION = 2


def launch_permitted(records, run, *, now, purpose):
    if run["active_child_run_id"] is not None:
        return False
    if records.find("artifact_intents", state="pending"):
        return False
    task = required(records, "tasks", id=run["task_id"])
    if task["user_excluded"] or task["cancel_requested"] or task["stop_requested"]:
        return False
    if task["mode"] == "diagnose":
        permission = records.one("diagnostic_authorizations", task_id=task["id"])
        return bool(
            permission
            and permission["state"] == "running"
            and permission["run_id"] == run["id"]
            and now < permission["deadline"]
            and permission["policy_revision"] == run["admission_revision"]
            and permission["policy_revision"] == DIAGNOSTIC_POLICY_REVISION
            and purpose == DIAGNOSTIC_PURPOSE[permission["kind"]]
        )
    repository = required(records, "repositories", id=task["repository_id"])
    return not (required(records, "global_control", singleton=1)["paused"] or repository["paused"])
