"""冻结候选、执行证据与独审记录绑定原任务的版本。"""

import json
from hashlib import sha256
from uuid import uuid4

from repowatch.application.acceptance import acceptance_view
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.domain.verification import ProbePlan, ReviewDecision, probe_verdict


def file_manifest(files):
    return [
        {"path": path, "mode": mode, "sha256": sha256(content).hexdigest(), "size": len(content)}
        for path, (mode, content) in sorted(files.items())
    ]


class RepairEvidence:
    def __init__(self, services, workspaces):
        self.services, self.workspaces = services, workspaces

    def freeze(self, token, workspace, *, parent_sha, message, base_sha=None):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            if any(
                item["state"] not in {"stopped", "isolated"}
                for item in records.find("command_states", run_id=token.run_id)
            ):
                raise PermissionError("命令未停止，不能冻结候选")
        files = self.workspaces.backend.snapshot(workspace, workspace["task_id"])
        manifest = file_manifest(files)
        commit = self.workspaces.backend.objects.commit_files(
            workspace["repository_id"], parent_sha, files, message=message
        )
        with services.store.write() as records:
            run = execution_record(records, token, services.clock())
            task = required(records, "tasks", id=run["task_id"])
            repository = required(records, "repositories", id=task["repository_id"])
            acceptance = acceptance_view(records, task)
            if (
                not acceptance
                or not acceptance["current"]
                or (acceptance["kind"] != "bug" and not acceptance["approved"])
            ):
                raise PermissionError("验收规范未确认或已失效")
            frozen = {
                **commit,
                "manifest": manifest,
                "manifest_sha256": content_digest(manifest),
                "base_sha": base_sha or parent_sha,
                "previous_head_sha": parent_sha if base_sha and base_sha != parent_sha else None,
                "input_revision": task["input_revision"],
                "feedback_revision": task["feedback_revision"],
                "acceptance_revision": task["acceptance_revision"],
                "admission_revision": repository["admission_revision"],
                "target_revision": repository["target_revision"],
                "import_epoch": repository["import_epoch"],
                "configuration_sha256": content_digest(json.loads(task["configuration_json"])),
            }
            snapshot = {**workspace, "mode": "review"}
            records.change(
                "workspaces", {"id": workspace["id"]}, {"snapshot_json": canonical_json(snapshot)}
            )
            identifier = str(uuid4())
            records.add(
                "repair_sessions",
                {
                    "id": identifier,
                    "task_id": task["id"],
                    "run_id": token.run_id,
                    "workspace_id": workspace["id"],
                    "base_sha": parent_sha,
                    "state": "frozen",
                    "snapshot_json": canonical_json(json.loads(run["snapshot_json"])),
                    "frozen_json": canonical_json(frozen),
                    "created_at": services.clock(),
                },
            )
            business_event(
                records, "candidate_frozen", task["id"], session_id=identifier, sha=commit["sha"]
            )
        return identifier, frozen

    def require_current(self, token, session_id):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            session = dict(required(records, "repair_sessions", id=session_id))
            task = required(records, "tasks", id=run["task_id"])
            repository = required(records, "repositories", id=task["repository_id"])
            frozen = json.loads(session["frozen_json"])
            if session["task_id"] != task["id"] or session["state"] not in {"frozen", "verified"}:
                raise PermissionError("候选不属于当前任务或已失效")
            for key in ("input_revision", "feedback_revision", "acceptance_revision"):
                if frozen[key] != task[key]:
                    raise PermissionError("任务输入或验收已变化")
            for key in ("admission_revision", "target_revision", "import_epoch"):
                if frozen[key] != repository[key]:
                    raise PermissionError("仓库配置或导入版本已变化")
            if frozen["base_sha"] != repository["target_sha"] or frozen[
                "configuration_sha256"
            ] != content_digest(json.loads(task["configuration_json"])):
                raise PermissionError("base 或环境已变化")
        workspace = self.workspaces.require(token, session["workspace_id"], now=services.clock())
        actual = self.workspaces.backend.snapshot(workspace, task["id"])
        if content_digest(file_manifest(actual)) != frozen["manifest_sha256"]:
            raise PermissionError("冻结后实际内容变化，旧证据失效")
        if session["state"] == "verified":
            for identifier in json.loads(session["evidence_json"])["artifact_ids"]:
                services.artifacts.require(identifier, now=services.clock())
        return session, frozen

    def confirm(self, token, session_id, evidence):
        session, frozen = self.require_current(token, session_id)
        if (
            not evidence.get("passed")
            or not evidence.get("review_passed")
            or evidence.get("review_run_id") == token.run_id
        ):
            raise PermissionError("缺少有效验证或独立审查")
        probe = ProbePlan.model_validate(evidence.get("probe"))
        review = ReviewDecision.model_validate(evidence.get("review"))
        if not review.passed or not review.probe_valid or review.weakened_acceptance:
            raise PermissionError("独审结论未通过")
        for side, key in (("base", "base_probe"), ("candidate", "candidate_probe")):
            result = evidence.get(key)
            if (
                not isinstance(result, dict)
                or result.get("log_artifact_id") not in evidence["artifact_ids"]
            ):
                raise PermissionError("缺少两侧真实探针日志")
            artifact = self.services.artifacts.require(
                result["log_artifact_id"], now=self.services.clock()
            )
            log = (self.services.artifacts.root / artifact["relative_path"]).read_text(
                encoding="utf-8", errors="replace"
            )
            expected = "target_failed" if side == "base" else "passed"
            if probe_verdict(result, log, probe, side=side) != expected:
                raise PermissionError("两侧探针未提供有效前失败后通过证明")
            if side == "candidate" and result.get("sha") != frozen["sha"]:
                raise PermissionError("候选探针版本不匹配")
        with self.services.store.read() as records:
            review_run = required(records, "runs", id=evidence["review_run_id"])
            if (
                review_run["task_id"] != session["task_id"]
                or review_run["parent_run_id"] != token.run_id
                or review_run["state"] != "ended"
                or json.loads(review_run["snapshot_json"])["mode"] != "review"
            ):
                raise PermissionError("独审运行不属于当前候选任务")
            review_artifacts = [
                required(records, "artifacts", id=identifier)
                for identifier in evidence["artifact_ids"]
                if required(records, "artifacts", id=identifier)["run_id"] == review_run["id"]
            ]
            matched_reviews = [
                item
                for item in review_artifacts
                if json.loads(item["context_json"]).get("producer") == "independent_review"
                and json.loads(item["context_json"]).get("candidate_sha") == frozen["sha"]
            ]
            if len(matched_reviews) != 1:
                raise PermissionError("独审缺少与当前运行绑定的实际产物")
        review_artifact = self.services.artifacts.require(
            matched_reviews[0]["id"], now=self.services.clock()
        )
        saved_review = json.loads(
            (self.services.artifacts.root / review_artifact["relative_path"]).read_text("utf-8")
        )
        if saved_review != evidence["review"]:
            raise PermissionError("独审结论与不可变产物不一致")
        for identifier in evidence["artifact_ids"]:
            self.services.artifacts.require(identifier, now=self.services.clock())
        with self.services.store.write() as records:
            execution_record(records, token, self.services.clock())
            records.change(
                "repair_sessions",
                {"id": session_id},
                {"state": "verified", "evidence_json": canonical_json(evidence)},
            )
            source = content_digest({"frozen": frozen, "evidence": evidence})
            identity = {
                "task_id": session["task_id"],
                "source_sha256": source,
                "feedback_revision": frozen["feedback_revision"],
            }
            if not records.one("distillation_intents", **identity):
                records.add(
                    "distillation_intents",
                    {"id": str(uuid4()), "session_id": session_id, **identity},
                )
            business_event(records, "repair_verified", session["task_id"], session_id=session_id)
