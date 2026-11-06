"""验收内容及人工批准只适用于对应输入、准入策略和目标配置。"""

import json

from repowatch.application.storage import ConflictError
from repowatch.domain.task_records import canonical_json
from repowatch.security import DEFAULT_REDACTOR


def acceptance_current(specification, task, repository):
    return bool(
        specification
        and specification["revision"] == task["acceptance_revision"]
        and specification["input_revision"] == task["input_revision"]
        and (
            specification["approved_feedback_revision"]
            if specification["approved_revision"] is not None
            else specification["feedback_revision"]
        )
        == task["feedback_revision"]
        and specification["admission_revision"] == repository["admission_revision"]
        and specification["target_revision"] == repository["target_revision"]
    )


def acceptance_view(records, task):
    specification = records.one(
        "task_acceptance_specs", task_id=task["id"], revision=task["acceptance_revision"]
    )
    if specification is None:
        return None
    repository = records.one("repositories", id=task["repository_id"])
    current = acceptance_current(specification, task, repository)
    return {
        **{key: value for key, value in specification.items() if key != "content_json"},
        "content": json.loads(specification["content_json"]),
        "current": current,
        "approved": current and specification["approved_revision"] == specification["revision"],
    }


def validate_content(kind, content):
    if kind not in {"bug", "feature", "refactor"} or not isinstance(content, dict):
        raise ValueError("验收规范类型或内容无效")
    fields = {"objective"}
    if kind in {"feature", "refactor"}:
        fields.add("plan")
    if kind == "refactor":
        fields.update({"structural_goal", "behavior_preservation"})
    if any(not isinstance(content.get(key), str) or not content[key].strip() for key in fields):
        raise ValueError("验收规范缺少明确目标、方案或重构保持条件")
    criteria = content.get("criteria")
    if (
        not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(item, str) or not item.strip() for item in criteria)
    ):
        raise ValueError("验收规范须包含可核查的验收条件列表")
    if DEFAULT_REDACTOR.clean(content) != content:
        raise ValueError("验收规范不能包含凭据")
    return canonical_json(content)


def record_proposal(records, task, repository, proposal):
    current = records.one(
        "task_acceptance_specs", task_id=task["id"], revision=task["acceptance_revision"]
    )
    if acceptance_current(current, task, repository):
        return task["acceptance_revision"]
    encoded = validate_content(proposal["kind"], proposal["content"])
    revision = task["acceptance_revision"] + 1
    records.add(
        "task_acceptance_specs",
        {
            "task_id": task["id"],
            "revision": revision,
            "kind": proposal["kind"],
            "content_json": encoded,
            "input_revision": task["input_revision"],
            "feedback_revision": task["feedback_revision"],
            "admission_revision": repository["admission_revision"],
            "target_revision": repository["target_revision"],
        },
    )
    return revision


def operate_acceptance(records, task, action, payload, actor):
    if actor != "local-user":
        raise PermissionError("只有本地用户可确认验收规范")
    if task["state"] == "running":
        raise ConflictError("旧运行尚未停止，不能修改或批准验收规范")
    if task["user_excluded"] or task["cancel_requested"] or task["state"] in {"ended", "cancelled"}:
        raise ConflictError("任务已排除、取消或结束，请先通过对应入口核对")
    if task["repository_id"] is None or task["mode"] in {"scan", "diagnose", "distill"}:
        raise PermissionError("当前任务模式不支持修复验收规范")
    repository = records.one("repositories", id=task["repository_id"])
    specification = records.one(
        "task_acceptance_specs", task_id=task["id"], revision=task["acceptance_revision"]
    )
    if action == "set_acceptance":
        if set(payload) != {"kind", "content"}:
            raise ValueError("验收规范提交字段无效")
        kind = payload["kind"]
        encoded = validate_content(kind, payload["content"])
        if (
            acceptance_current(specification, task, repository)
            and specification["kind"] == kind
            and specification["content_json"] == encoded
        ):
            raise ConflictError("验收规范没有变化")
        revision = task["acceptance_revision"] + 1
        records.add(
            "task_acceptance_specs",
            {
                "task_id": task["id"],
                "revision": revision,
                "kind": kind,
                "content_json": encoded,
                "input_revision": task["input_revision"],
                "feedback_revision": task["feedback_revision"] + 1,
                "admission_revision": repository["admission_revision"],
                "target_revision": repository["target_revision"],
            },
        )
        changes = {"acceptance_revision": revision}
        if task["state"] != "blocked":
            changes.update(state="queued", mode="triage", wait_reason=None, outcome=None)
            if kind in {"feature", "refactor"}:
                changes.update(state="waiting", wait_reason="approval")
        return (
            changes,
            "acceptance_spec_changed",
            {
                "acceptance_revision": revision,
                "acceptance_kind": kind,
            },
        )
    if payload:
        raise ValueError("批准仅绑定当前任务及验收修订，不接收额外字段")
    if not acceptance_current(specification, task, repository):
        raise ConflictError("验收规范已过期，请按最新输入和策略保存新修订")
    if specification["approved_revision"] is not None:
        raise ConflictError("当前验收修订已经批准")
    records.change(
        "task_acceptance_specs",
        {"task_id": task["id"], "revision": task["acceptance_revision"]},
        {
            "approved_revision": task["acceptance_revision"],
            "approved_by": actor,
            "approved_feedback_revision": task["feedback_revision"] + 1,
        },
    )
    changes = {}
    if task["state"] == "waiting" and task["wait_reason"] == "approval":
        changes.update(state="queued", mode="triage", wait_reason=None, outcome=None)
    return (
        changes,
        "acceptance_spec_approved",
        {
            "acceptance_revision": task["acceptance_revision"],
            "actor": actor,
        },
    )
