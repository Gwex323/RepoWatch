import json
from uuid import uuid4

from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.task_records import canonical_json


class WorkspaceService:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend

    def create(self, token, sha, *, image="alpine/git:latest", now):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            task = required(records, "tasks", id=run["task_id"])
            task_id, repository_id = task["id"], task["repository_id"]
            mode = "implement" if task["mode"] in {"implement", "amend"} else "scan"
        result = self.backend.create(repository_id, sha, task_id=task_id, mode=mode, image=image)
        with self.store.write() as records:
            execution_record(records, token, now)
            business_event(records, "workspace_preflight", task_id, result=result)
        if result["ready"]:
            with self.store.write() as records:
                execution_record(records, token, now)
                records.add(
                    "workspaces",
                    {
                        "id": result["id"],
                        "task_id": task_id,
                        "repository_id": repository_id,
                        "snapshot_json": canonical_json(result),
                        "state": "ready",
                        "created_at": now,
                    },
                )
        return result

    def require(self, token, identifier, *, now):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            workspace = required(records, "workspaces", id=identifier)
            if workspace["task_id"] != run["task_id"] or workspace["state"] != "ready":
                raise PermissionError("工作区归属不符或已失效")
            result = json.loads(workspace["snapshot_json"])
        if hasattr(self.backend, "exists") and not self.backend.exists(result):
            with self.store.write() as records:
                execution_record(records, token, now)
                records.change("workspaces", {"id": identifier}, {"state": "unavailable"})
            raise PermissionError("工作区实际卷不存在，已撤销就绪")
        return result

    def rebuild(self, token, identifier, *, now):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            if any(
                row["state"] not in {"stopped", "isolated"}
                for row in records.find("command_states", run_id=run["id"])
            ):
                raise PermissionError("旧命令尚未核实停止，不能重建")
        previous = self.require(token, identifier, now=now)
        result = self.create(token, previous["sha"], image=previous["image"], now=now)
        if result["ready"]:
            self.remove(token, identifier, now=now)
        return result

    def remove(self, token, identifier, *, now):
        with self.store.write() as records:
            run = execution_record(records, token, now)
            workspace_row = required(records, "workspaces", id=identifier)
            if workspace_row["task_id"] != run["task_id"]:
                raise PermissionError("工作区归属不符")
            if workspace_row["state"] == "removed":
                return
            if any(
                row["state"] not in {"stopped", "isolated"}
                for row in records.find("command_states", run_id=run["id"])
            ):
                raise PermissionError("旧命令未停止，不能删除工作区")
            workspace = json.loads(workspace_row["snapshot_json"])
            records.change("workspaces", {"id": identifier}, {"state": "unavailable"})
        if not hasattr(self.backend, "exists") or self.backend.exists(workspace):
            self.backend.remove(workspace)
        with self.store.write() as records:
            execution_record(records, token, now)
            records.change("workspaces", {"id": identifier}, {"state": "removed"})


def command_identity(workspace, argv, timeout):
    if (
        not argv
        or any(not isinstance(argument, str) or "\0" in argument for argument in argv)
        or not 0 < timeout <= 3600
    ):
        raise ValueError("检查需要明确命令及有限超时")
    name = "repowatch-command-" + uuid4().hex
    return {
        "kind": "docker",
        "container": name,
        "workspace_id": workspace["id"],
        "argv": list(argv),
        "timeout": timeout,
    }
