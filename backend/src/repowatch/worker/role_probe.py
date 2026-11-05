"""隔离会话中的实际角色工具／交接核验，仅修改内存能力夹具。"""

import json
import secrets
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from repowatch.adapters.providers import ModelLimits, ProviderRequestError
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.credentials import CredentialError
from repowatch.application.role_snapshots import capability_scope
from repowatch.domain.budget import BudgetAmounts, BudgetDenied
from repowatch.domain.providers import ProviderModel
from repowatch.domain.task_records import canonical_json, content_digest

FIXED = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a - b\n"


def tool_schema(name, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Trusted in-memory role capability fixture: " + name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


class RoleProbe:
    def __init__(self, diagnostics, adapter, budgets, orchestrator, artifacts, roles, *, clock):
        self.diagnostics, self.adapter = diagnostics, adapter
        self.orchestrator, self.artifacts, self.roles, self.clock = (
            orchestrator,
            artifacts,
            roles,
            clock,
        )
        self.executor = BudgetedExecutor(budgets, orchestrator, clock=clock)

    def run(self, token):
        specification = self.diagnostics.specification(token, now=self.clock())
        if specification["kind"] != "provider_capability":
            raise PermissionError("只有显式角色能力诊断可进入此处理器")
        parameters = specification["parameters"]
        selection = ProviderModel(
            model=parameters["model"],
            reasoning_effort=parameters.get("reasoning_effort"),
        )
        revision = self.adapter.service.revision(parameters["provider_revision_id"])
        resolved = {
            "role": parameters["role"],
            "selection": selection.model_dump(mode="json"),
            "provider_id": revision["provider_id"],
            "provider_revision_id": revision["id"],
            "configuration_sha256": content_digest(json.loads(revision["configuration_json"])),
            "credential_reference": revision["credential_reference"],
            "credential_revision": revision["credential_revision"],
        }
        scope = capability_scope(resolved, adapter_revision=self.roles.adapter_revision)
        session_id, proof = str(uuid4()), secrets.token_hex(24)
        role = parameters["role"]
        required_tools = {
            "read_fixture",
            "write_fixture" if role == "processor" else "read_evidence",
        }
        schemas = [tool_schema("read_fixture", {}, [])]
        if role == "processor":
            schemas.append(
                tool_schema("write_fixture", {"content": {"type": "string"}}, ["content"])
            )
        else:
            schemas.append(tool_schema("read_evidence", {}, []))
        schemas.append(
            tool_schema(
                "RoleProbeResult",
                {key: {"type": "string"} for key in ("session_id", "role", "proof")},
                ["session_id", "role", "proof"],
            )
        )
        messages = [
            SystemMessage(
                content=(
                    "This is a trusted in-memory capability fixture, not a repository task. "
                    "Call read_fixture first. Processor: call write_fixture with the fixed source "
                    "returned by read_fixture. Reviewer: call read_evidence, without writing. "
                    "Then call RoleProbeResult with the exact session_id, role and proof returned "
                    "by the tools. Call one tool at a time; do not finish with plain text."
                )
            ),
            HumanMessage(content=canonical_json({"session_id": session_id, "role": role})),
        ]
        observed, matched = set(), False
        remaining = specification["deadline"] - self.clock()
        if remaining <= 0:
            raise PermissionError("诊断已到期")
        try:
            with self.adapter.session(
                revision["id"],
                selection,
                ModelLimits(timeout_seconds=min(20, remaining)),
            ) as model:
                bound = model.bind_tools(schemas)
                for attempt in range(4):
                    remaining = specification["deadline"] - self.clock()
                    if remaining <= 0:
                        break
                    self.orchestrator.heartbeat(token, now=self.clock(), ttl=min(30, remaining))
                    answer = self.executor.execute(
                        token,
                        f"role-probe:{attempt}",
                        BudgetAmounts(
                            active_ms=max(1, round(min(20, remaining) * 1000)),
                            model_calls=1,
                            tokens=22048,
                        ),
                        lambda: bound.invoke(messages),
                        self._usage,
                    )
                    messages.append(answer)
                    if len(answer.tool_calls) != 1:
                        break
                    call = answer.tool_calls[0]
                    name, arguments = call["name"], call["args"]
                    if name == "RoleProbeResult":
                        matched = arguments == {
                            "session_id": session_id,
                            "role": role,
                            "proof": proof,
                        }
                        matched = matched and observed == required_tools
                        break
                    if name not in required_tools or name in observed:
                        break
                    if name == "read_fixture" and arguments == {}:
                        result = {
                            "content": BROKEN if role == "processor" else FIXED,
                            "fixed": FIXED,
                        }
                    elif (
                        name == "write_fixture"
                        and "read_fixture" in observed
                        and arguments == {"content": FIXED}
                    ):
                        result = {"written": True}
                    elif name == "read_evidence" and "read_fixture" in observed and arguments == {}:
                        result = {"fixture_checked": True, "expected_output": 3}
                    else:
                        break
                    observed.add(name)
                    result.update(session_id=session_id, role=role, proof=proof)
                    messages.append(
                        ToolMessage(content=canonical_json(result), tool_call_id=call["id"])
                    )
        except BudgetDenied:
            raise
        except (ProviderRequestError, CredentialError, ValueError, PermissionError):
            matched = False
        evidence = {
            "producer": "host_role_probe",
            "scope": scope,
            "tools": sorted(observed),
            "handoff_matched": matched,
        }
        artifact_id = self.artifacts.register(
            token,
            "role-probe-result",
            canonical_json(evidence).encode(),
            context={"producer": "host_role_probe"},
            now=self.clock(),
        )
        result = self.diagnostics.finish(
            token,
            now=self.clock(),
            passed=matched,
            checks={"tool_calling": observed == required_tools, "structured_output": matched},
        )
        with self.diagnostics.store.read() as records:
            task_id = records.one("runs", id=token.run_id)["task_id"]
        self.roles.record_capability(task_id, artifact_id, now=self.clock())
        return result

    @staticmethod
    def _usage(answer):
        actual = {"model_calls": 1}
        tokens = (answer.usage_metadata or {}).get("total_tokens")
        if type(tokens) is int and tokens >= 0:
            actual["tokens"] = tokens
        return actual
