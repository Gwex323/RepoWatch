"""业务阶段结果先核实归档；图检查点不能替代真实结果。"""

import json

from repowatch.application.storage import ConflictError
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.task_records import canonical_json, content_digest, stable_id


class StageResults:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def begin(self, token, stage_key, *, now):
        self.store.assert_execution_available()
        if not stage_key:
            raise ValueError("阶段必须有稳定标识")
        identifier = stable_id("stage", token.run_id, stage_key)
        with self.store.write() as records:
            run = execution_record(records, token, now)
            if records.one("stage_intents", id=identifier) is None:
                records.add(
                    "stage_intents",
                    {
                        "id": identifier,
                        "run_id": token.run_id,
                        "stage_key": stage_key,
                        "thread_id": run["graph_thread_id"],
                        "state": "intent",
                        "context_json": canonical_json(
                            {
                                "snapshot_sha256": content_digest(json.loads(run["snapshot_json"])),
                            }
                        ),
                    },
                )
            return identifier

    def save(self, token, stage_id, *, artifact_ids, result, now):
        self.store.assert_execution_available()
        if not artifact_ids or len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("阶段结果必须引用已登记的实际产物")
        artifacts = [
            self.artifacts.require(item, run_id=token.run_id, now=now) for item in artifact_ids
        ]
        with self.store.write() as records:
            run = execution_record(records, token, now)
            stage = required(records, "stage_intents", id=stage_id)
            if stage["run_id"] != token.run_id:
                raise PermissionError("阶段不属于当前运行")
            for artifact in artifacts:
                if dict(required(records, "artifacts", id=artifact["id"])) != artifact:
                    raise ConflictError("核对后产物记录已变化")
            payload = canonical_json({"artifact_ids": artifact_ids, "result": result})
            if stage["state"] != "intent":
                if stage["result_json"] != payload:
                    raise ConflictError("已保存的阶段结果不可覆盖")
                return
            records.change(
                "stage_intents",
                {"id": stage_id},
                {
                    "state": "result_saved",
                    "result_json": payload,
                },
            )
            business_event(records, "stage_result_saved", run["task_id"], stage_id=stage_id)
