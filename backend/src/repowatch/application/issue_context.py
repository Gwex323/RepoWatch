"""完整远端事实、版本化人工关系决定和修复入口守卫。"""

import json
from uuid import uuid4

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.application.budgets import BudgetedExecutor, remember_operation, replay_operation
from repowatch.application.github_reads import issue_input
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import (
    bump_task,
    business_event,
    execution_record,
    required,
)
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.security import DEFAULT_REDACTOR


def relation_fingerprint(kind, values):
    """Stable projection for relation changes; ignores volatile GitHub fields."""
    if kind == "comments":
        return [
            {
                "id": item.get("id"),
                "body": item.get("body"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "user": (item.get("user") or {}).get("login"),
            }
            for item in values
        ]
    return [
        {
            "id": item.get("id"),
            "number": item.get("number"),
            "state": item.get("state"),
            "title": item.get("title"),
            "body": item.get("body"),
            "merged": item.get("merged"),
            "merged_at": item.get("merged_at"),
            "head": (item.get("head") or {}).get("sha"),
            "head_ref": (item.get("head") or {}).get("ref"),
            "base_ref": (item.get("base") or {}).get("ref"),
            "base_sha": (item.get("base") or {}).get("sha"),
        }
        for item in values
    ]


class IssueContexts:
    def __init__(self, services):
        self.services = services

    def read(self, token):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
            inputs = json.loads(task["input_json"])
        if task["issue_id"] is None:
            return None
        number = inputs.get("number")
        if type(number) is not int:
            raise PermissionError("Issue 编号未确认，不能读取或发布")
        port = GitHubReadAdapter(
            vault=services.vault, credential_reference=repository["credential_reference"]
        )
        executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)
        facts = executor.execute(
            token,
            "issue-context:" + uuid4().hex,
            BudgetAmounts(active_ms=180000),
            lambda: port.issue_context(
                repository["github_owner"], repository["github_name"], number
            ),
            lambda _: {},
            purpose="tool",
        )
        if str(facts["issue"]["id"]) != task["issue_id"]:
            raise PermissionError("远端 Issue 稳定身份不匹配")
        if facts.get("complete") is not True:
            raise PermissionError("远端 Issue／PR 读取不完整，不能按无外部修复放行")
        normalized = issue_input(facts["issue"])
        if any(inputs.get(key) != value for key, value in normalized.items()):
            services.tasks.observe(
                task["id"],
                "github:issue:" + task["issue_id"],
                content_digest(normalized),
                normalized,
                now=services.clock(),
            )
            raise ConflictError("Issue 输入已变化，必须重新分诊")
        with services.store.read() as records:
            previous = records.find("issue_contexts", task_id=task["id"])
            latest = max(previous, key=lambda item: item["created_at"], default=None)
        changed_relations = False
        if latest:
            prior_facts = json.loads(latest["facts_json"])
            for kind in ("comments", "pull_requests"):
                cleaned_values = DEFAULT_REDACTOR.clean(facts[kind])
                if content_digest(relation_fingerprint(kind, cleaned_values)) == content_digest(
                    relation_fingerprint(kind, prior_facts[kind])
                ):
                    continue
                services.tasks.observe(
                    task["id"],
                    "github:" + kind + ":" + task["issue_id"],
                    content_digest({"values": cleaned_values, "after": latest["digest"]}),
                    {kind: cleaned_values},
                    kind="feedback",
                    now=services.clock(),
                )
                changed_relations = True
        cleaned = DEFAULT_REDACTOR.clean(facts)
        digest = content_digest(cleaned)
        with services.store.read() as records:
            old = records.one("issue_contexts", task_id=task["id"], digest=digest)
        if old is None:
            artifact_id = services.artifacts.register(
                token,
                "issue-context:" + digest,
                canonical_json(cleaned).encode(),
                context={"producer": "github_issue_context", "digest": digest},
                now=services.clock(),
            )
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.add(
                    "issue_contexts",
                    {
                        "id": str(uuid4()),
                        "task_id": task["id"],
                        "run_id": token.run_id,
                        "digest": digest,
                        "artifact_id": artifact_id,
                        "facts_json": canonical_json(cleaned),
                        "created_at": services.clock(),
                    },
                )
        if changed_relations:
            raise ConflictError("相关评论或 PR 已变化，必须重新分诊并核对方案批准")
        with services.store.read() as records:
            decisions = {
                item["relation_key"]: item["decision"]
                for item in records.find(
                    "relation_decisions", task_id=task["id"], context_digest=digest
                )
            }
            managed = records.one("managed_pull_requests", task_id=task["id"])
        return {
            "digest": digest,
            "facts": cleaned,
            "decisions": decisions,
            "managed_pr_id": managed["pr_id"] if managed else None,
        }

    def decide(
        self,
        operation_id,
        task_id,
        *,
        expected_revision,
        context_digest,
        relation_key,
        decision,
        explanation,
    ):
        if (
            decision not in {"same", "unrelated", "occupied", "available"}
            or not isinstance(explanation, str)
            or not explanation.strip()
        ):
            raise ValueError("关系决定与依据无效")
        request = {
            "kind": "relation",
            "task_id": task_id,
            "revision": expected_revision,
            "digest": context_digest,
            "key": relation_key,
            "decision": decision,
            "explanation": explanation,
        }
        with self.services.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            task = required(records, "tasks", id=task_id)
            contexts = records.find("issue_contexts", task_id=task_id)
            latest = max(contexts, key=lambda item: item["created_at"], default=None)
            if (
                task["revision"] != expected_revision
                or latest is None
                or latest["digest"] != context_digest
                or task["state"] == "running"
            ):
                raise ConflictError("关系依据已变化，请重新读取")
            facts = json.loads(latest["facts_json"])
            keys = {"pr:" + str(item["id"]) for item in facts["pull_requests"]} | {"ownership"}
            if relation_key not in keys or (relation_key == "ownership") != (
                decision in {"occupied", "available"}
            ):
                raise ValueError("关系决定不适用于该对象")
            records.add(
                "relation_decisions",
                {
                    "task_id": task_id,
                    "context_digest": context_digest,
                    "relation_key": relation_key,
                    "decision": decision,
                    "evidence_json": canonical_json(
                        DEFAULT_REDACTOR.clean({"explanation": explanation})
                    ),
                    "actor": "local-user",
                },
            )
            if (
                not task["user_excluded"]
                and not task["cancel_requested"]
                and task["state"] != "blocked"
            ):
                bump_task(
                    records,
                    task,
                    state="queued",
                    mode="triage",
                    wait_reason=None,
                    outcome=None,
                    feedback_revision=task["feedback_revision"] + 1,
                )
                updated = required(records, "tasks", id=task_id)
                records.add(
                    "task_input_versions",
                    {
                        "task_id": task_id,
                        "input_revision": updated["input_revision"],
                        "feedback_revision": updated["feedback_revision"],
                        "input_json": updated["input_json"],
                    },
                )
            result = {"task_id": task_id, "decision": decision}
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records, "relation_decided", task_id, relation_key=relation_key, decision=decision
            )
            return result
