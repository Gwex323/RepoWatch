"""在领取事务中解析角色；历史 Run 只使用固化引用。"""

import json
import re

from repowatch.domain.providers import ProviderModel
from repowatch.domain.task_records import content_digest

ROLE_TOOL_REVISION = 1


def validate_binding(key, definition):
    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", key):
        raise ValueError("角色绑定标识无效")
    if not isinstance(definition, dict) or set(definition) != {"role", "provider_id", "selection"}:
        raise ValueError("角色绑定必须指定角色、供应商及模型")
    role = definition["role"]
    if role not in {"processor", "reviewer"} or (key in {"processor", "reviewer"} and key != role):
        raise ValueError("角色与绑定类型不一致")
    ProviderModel.model_validate(definition["selection"])


def resolve_role(records, definition):
    provider = records.one("providers", id=definition["provider_id"])
    if not provider or provider["state"] != "active":
        raise PermissionError("角色供应商尚未激活")
    revision = records.one("provider_revisions", id=provider["active_revision_id"])
    return {
        "role": definition["role"],
        "selection": definition["selection"],
        "provider_id": provider["id"],
        "provider_revision_id": revision["id"],
        "configuration": json.loads(revision["configuration_json"]),
        "configuration_sha256": content_digest(json.loads(revision["configuration_json"])),
        "credential_reference": revision["credential_reference"],
        "credential_revision": revision["credential_revision"],
    }


def resolve_run_roles(records, task_configuration):
    definitions = {
        row["binding_key"]: (json.loads(row["definition_json"]), row["revision"])
        for row in records.find("role_bindings")
    }
    overrides = task_configuration.get("roles", {})
    if not isinstance(overrides, dict):
        raise ValueError("任务角色配置无效")
    for key, definition in overrides.items():
        validate_binding(key, definition)
        definitions[key] = (definition, None)
    return {
        key: {**resolve_role(records, definition), "binding_revision": revision}
        for key, (definition, revision) in definitions.items()
    }


def capability_scope(resolved, *, adapter_revision):
    return {
        key: resolved[key]
        for key in (
            "role",
            "selection",
            "provider_id",
            "provider_revision_id",
            "configuration_sha256",
            "credential_reference",
            "credential_revision",
        )
    } | {"adapter_revision": adapter_revision, "tool_revision": ROLE_TOOL_REVISION}
