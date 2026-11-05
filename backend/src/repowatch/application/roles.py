"""显式角色绑定、不可变运行引用与有限期能力准入。"""

import json

from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.role_snapshots import capability_scope, validate_binding
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, business_event, execution_record, required
from repowatch.domain.task_records import canonical_json, content_digest, require_time
from repowatch.security import DEFAULT_REDACTOR


class RoleService:
    def __init__(self, store, artifacts, *, adapter_revision=1, capability_ttl=86400):
        if type(capability_ttl) is not int or not 1 <= capability_ttl <= 604800:
            raise ValueError("能力有效期必须有限且不超过七天")
        self.store, self.artifacts, self.adapter_revision = store, artifacts, adapter_revision
        self.capability_ttl = capability_ttl

    def set_binding(
        self,
        operation_id,
        key,
        definition,
        *,
        expected_revision,
        actor="local-user",
    ):
        human(actor)
        validate_binding(key, definition)
        if DEFAULT_REDACTOR.clean(definition) != definition:
            raise ValueError("角色配置不能包含凭据")
        request = {
            "kind": "role_binding",
            "key": key,
            "definition": definition,
            "expected_revision": expected_revision,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            existing = records.one("role_bindings", binding_key=key)
            actual = existing["revision"] if existing else 0
            if type(expected_revision) is not int or actual != expected_revision:
                raise ConflictError("角色配置修订已变化，请刷新")
            provider = required(records, "providers", id=definition["provider_id"])
            if provider["state"] != "active":
                raise PermissionError("供应商尚未激活")
            revision = actual + 1
            values = {"revision": revision, "definition_json": canonical_json(definition)}
            if existing:
                records.change("role_bindings", {"binding_key": key}, values)
            else:
                records.add("role_bindings", {"binding_key": key, **values})
            result = {"binding_key": key, "revision": revision, "definition": definition}
            remember_operation(records, operation_id, fingerprint, revision, result)
            records.add(
                "role_binding_versions",
                {
                    "binding_key": key,
                    **values,
                    "operation_id": operation_id,
                },
            )
            business_event(records, "role_binding_changed", binding_key=key, revision=revision)
            return result

    def list(self):
        with self.store.read() as records:
            return [
                {
                    "binding_key": row["binding_key"],
                    "revision": row["revision"],
                    "definition": json.loads(row["definition_json"]),
                }
                for row in records.find("role_bindings")
            ]

    def record_capability(self, diagnostic_task_id, artifact_id, *, now, ttl=None):
        ttl = self.capability_ttl if ttl is None else ttl
        require_time(now)
        if type(ttl) is not int or not 1 <= ttl <= 604800:
            raise ValueError("能力有效期必须有限且不超过七天")
        artifact = self.artifacts.require(artifact_id, now=now)
        # Only the host role probe writes this typed evidence. Never accept an API flag as proof.
        path = self.artifacts.root / artifact["relative_path"]
        if path.stat().st_size > 100_000:
            raise ValueError("能力证据过大")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict) or evidence.get("producer") != "host_role_probe":
            raise ValueError("缺少实际角色核验产物")
        with self.store.write() as records:
            permission = required(records, "diagnostic_authorizations", task_id=diagnostic_task_id)
            run = required(records, "runs", id=permission["run_id"])
            if (
                permission["kind"] != "provider_capability"
                or permission["state"] not in {"completed", "failed"}
                or artifact["run_id"] != run["id"]
                or run["state"] != "ended"
            ):
                raise PermissionError("能力证据不属于已完成的角色诊断")
            parameters = json.loads(permission["parameters_json"])
            scope = evidence.get("scope", {})
            if (
                scope.get("provider_revision_id") != parameters["provider_revision_id"]
                or scope.get("role") != parameters["role"]
                or scope.get("selection")
                != {
                    "model": parameters["model"],
                    "reasoning_effort": parameters.get("reasoning_effort"),
                }
                or scope.get("adapter_revision") != self.adapter_revision
            ):
                raise PermissionError("能力核验范围不匹配")
            checked = json.loads(permission["result_json"])
            required_tools = (
                {"read_fixture", "write_fixture"}
                if parameters["role"] == "processor"
                else {
                    "read_fixture",
                    "read_evidence",
                }
            )
            passed = bool(
                checked["passed"]
                and checked["checks"].get("tool_calling")
                and checked["checks"].get("structured_output")
                and evidence.get("handoff_matched") is True
                and set(evidence.get("tools", [])) == required_tools
            )
            existing = records.one("capability_checks", diagnostic_task_id=diagnostic_task_id)
            if existing:
                if existing["artifact_id"] != artifact_id:
                    raise ConflictError("同一诊断的能力证据不同")
                return dict(existing)
            records.add(
                "capability_checks",
                {
                    "diagnostic_task_id": diagnostic_task_id,
                    "artifact_id": artifact_id,
                    "scope_sha256": content_digest(scope),
                    "scope_json": canonical_json(scope),
                    "passed": int(passed),
                    "checked_at": run["ended_at"],
                    "expires_at": run["ended_at"] + ttl,
                },
            )
            business_event(records, "role_capability_checked", diagnostic_task_id, passed=passed)
            return dict(
                required(records, "capability_checks", diagnostic_task_id=diagnostic_task_id)
            )

    def admit(self, token, *, now, keys=("processor", "reviewer")):
        denial = None
        with self.store.write() as records:
            run = execution_record(records, token, now)
            roles = json.loads(run["snapshot_json"]).get("roles", {})
            for key in keys:
                resolved = roles.get(key)
                if resolved is None:
                    denial = "role_configuration_missing"
                    break
                scope = capability_scope(resolved, adapter_revision=self.adapter_revision)
                checks = records.find("capability_checks", scope_sha256=content_digest(scope))
                latest = max(checks, key=lambda row: row["sequence"], default=None)
                if (
                    not latest
                    or not latest["passed"]
                    or not latest["checked_at"] <= now < latest["expires_at"]
                ):
                    denial = "role_capability_missing_or_expired"
                    break
            if denial:
                block(records, denial, run["task_id"], now=now)
        if denial:
            raise PermissionError("角色能力未通过或已失效，需显式诊断并恢复")
        # Recheck archived bytes on each admission, not merely the database flag.
        for key in keys:
            scope = capability_scope(roles[key], adapter_revision=self.adapter_revision)
            with self.store.read() as records:
                latest = max(
                    records.find("capability_checks", scope_sha256=content_digest(scope)),
                    key=lambda row: row["sequence"],
                )
            self.artifacts.require(latest["artifact_id"], now=now)
        return roles
