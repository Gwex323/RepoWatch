"""先完整落盘再登记证据；文件引用不能自行成为验收通过。"""

import json
from hashlib import sha256
from pathlib import Path

from repowatch.adapters.storage.artifact_files import verify_file, write_immutable
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import block, business_event, execution_record, required
from repowatch.domain.task_records import canonical_json, content_digest, require_time, stable_id
from repowatch.security import DEFAULT_REDACTOR


class ArtifactService:
    def __init__(self, store, artifacts_dir: Path):
        self.store, self.root = store, artifacts_dir.resolve()

    def register(self, token, operation_key, data: bytes, *, context: dict, now):
        self.store.assert_execution_available()
        require_time(now)
        if not operation_key or not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
            raise ValueError("产物必须有稳定键且不超过 64 MiB；大型日志应分块归档")
        identifier = stable_id("artifact", token.run_id, operation_key)
        digest = sha256(data).hexdigest()
        relative = identifier + ".bin"
        with self.store.write() as records:
            run = execution_record(records, token, now)
            scope = {**context, "snapshot_sha256": content_digest(json.loads(run["snapshot_json"]))}
            identity = {
                "id": identifier,
                "run_id": run["id"],
                "operation_key": operation_key,
                "sha256": digest,
                "relative_path": relative,
                "context_json": canonical_json(scope),
                "byte_length": len(data),
            }
            intent = records.one("artifact_intents", id=identifier)
            if intent:
                if any(intent[key] != value for key, value in identity.items()):
                    raise ConflictError("同一产物操作内容不同，不能覆盖")
                if intent["state"] == "unavailable":
                    raise PermissionError("旧证据已失效；重建必须使用新产物操作键")
            else:
                records.add("artifact_intents", {**identity, "state": "pending"})
        if intent and intent["state"] == "registered":
            self.require(identifier, run_id=token.run_id, now=now)
            return identifier
        try:
            write_immutable(self.root, relative, data, digest)
            with self.store.write() as records:
                execution_record(records, token, now)
                existing = records.one("artifacts", id=identifier)
                if existing is None:
                    records.add(
                        "artifacts",
                        {
                            key: identity[key]
                            for key in ("id", "run_id", "sha256", "relative_path", "context_json")
                        },
                    )
                    business_event(
                        records, "artifact_registered", run["task_id"], artifact_id=identifier
                    )
                elif not existing["available"]:
                    raise PermissionError("产物已失效，不能重新激活")
                records.change("artifact_intents", {"id": identifier}, {"state": "registered"})
            return identifier
        except (OSError, StorageError):
            self.store.stop_execution()
            raise StorageError("必要产物未能持久登记，停止工具及发布并等待对账") from None

    def require(self, artifact_id, *, run_id=None, now):
        require_time(now)
        with self.store.read() as records:
            artifact = dict(required(records, "artifacts", id=artifact_id))
        if run_id is not None and artifact["run_id"] != run_id:
            raise PermissionError("产物不属于当前运行")
        valid = (
            artifact["available"]
            and verify_file(
                self.root,
                artifact["relative_path"],
                artifact["sha256"],
            )["valid"]
        )
        if not valid:
            with self.store.write() as records:
                records.change("artifacts", {"id": artifact_id}, {"available": 0})
                if records.one("artifact_intents", id=artifact_id):
                    records.change(
                        "artifact_intents", {"id": artifact_id}, {"state": "unavailable"}
                    )
                run = required(records, "runs", id=artifact["run_id"])
                block(
                    records,
                    "artifact_unavailable",
                    run["task_id"],
                    now=now,
                    artifact_id=artifact_id,
                )
            raise PermissionError("产物缺失或摘要变化，必须重建实际证据")
        return artifact

    def reconcile(self, token, operation_key, *, now):
        """显式核对残留落盘；只有完整文件可补登记，绝不合成内容。"""
        with self.store.read() as records:
            execution_record(records, token, now)
            intent = dict(
                required(
                    records,
                    "artifact_intents",
                    run_id=token.run_id,
                    operation_key=operation_key,
                )
            )
        valid = verify_file(self.root, intent["relative_path"], intent["sha256"])["valid"]
        with self.store.write() as records:
            execution_record(records, token, now)
            if not valid or intent["state"] == "unavailable":
                records.change("artifact_intents", {"id": intent["id"]}, {"state": "unavailable"})
                run = required(records, "runs", id=token.run_id)
                block(
                    records,
                    "artifact_unavailable",
                    run["task_id"],
                    now=now,
                    artifact_id=intent["id"],
                )
                return {"registered": False}
            if records.one("artifacts", id=intent["id"]) is None:
                records.add(
                    "artifacts",
                    {
                        key: intent[key]
                        for key in ("id", "run_id", "sha256", "relative_path", "context_json")
                    },
                )
                business_event(records, "artifact_reconciled", artifact_id=intent["id"])
            records.change("artifact_intents", {"id": intent["id"]}, {"state": "registered"})
        return {"registered": True, "artifact_id": intent["id"]}


class EventReader:
    def __init__(self, store):
        self.store = store

    def read(self, *, after=0, limit=100, task_id=None):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("事件游标及批量上限无效")
        with self.store.read() as records:
            rows = records.events(after=after, limit=limit, task_id=task_id)
            events = [
                {
                    "sequence": row["sequence"],
                    "task_id": row["task_id"],
                    "kind": row["kind"],
                    "payload": DEFAULT_REDACTOR.clean(json.loads(row["payload_json"])),
                }
                for row in rows
            ]
        return {"events": events, "cursor": events[-1]["sequence"] if events else after}
