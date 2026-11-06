"""候选查重与唯一 Issue 创建意图；先核实敏感状态，未知写入先对账。"""

import json
from uuid import uuid4

from repowatch.application.budgets import BudgetedExecutor, remember_operation, replay_operation
from repowatch.application.github_reads import issue_input
from repowatch.application.publication import GitHubPublisher
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import (
    bump_task,
    business_event,
    create_task,
    execution_record,
    required,
)
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.security import DEFAULT_REDACTOR


class CandidateIssues:
    def __init__(self, services):
        self.services = services

    def read(self, token):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
        port = GitHubPublisher(
            vault=services.vault, credential_reference=repository["credential_reference"]
        )
        prefix = port._path(repository["github_owner"], repository["github_name"])
        issues = BudgetedExecutor(services.budgets, services.queue, clock=services.clock).execute(
            token,
            "candidate-issues:" + uuid4().hex,
            BudgetAmounts(active_ms=180000),
            lambda: port.collection(prefix + "/issues", params={"state": "all"}),
            lambda _: {},
            purpose="tool",
        )
        issues = [item for item in issues if "pull_request" not in item]
        facts = DEFAULT_REDACTOR.clean(
            {
                "issues": issues,
                "input_revision": task["input_revision"],
                "admission_revision": repository["admission_revision"],
            }
        )
        digest = content_digest(facts)
        artifact = services.artifacts.register(
            token,
            "candidate-lookup:" + uuid4().hex,
            canonical_json(facts).encode(),
            context={"producer": "candidate_issue_lookup", "digest": digest},
            now=services.clock(),
        )
        return {
            "digest": digest,
            "facts": facts,
            "artifact_id": artifact,
            "port": port,
            "prefix": prefix,
        }

    def resolve(self, token, analysis, lookup, relation):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
            manual = records.one(
                "candidate_issue_decisions", task_id=task["id"], context_digest=lookup["digest"]
            )
            old_intent = records.one("publication_intents", task_id=task["id"], kind="issue")
        if analysis.sensitive:
            raise PermissionError("敏感候选未确认披露方式，不得创建公开 Issue")
        if manual:
            relation = {
                "relation": manual["decision"],
                "issue_number": manual["issue_number"],
                "evidence": json.loads(manual["evidence_json"]),
            }
        if old_intent and old_intent["state"] in {"dispatched", "unknown", "confirmed"}:
            request = json.loads(old_intent["request_json"])
            matches = [
                item
                for item in lookup["facts"]["issues"]
                if request["marker"] in (item.get("body") or "")
                and item.get("user", {}).get("login") == request["execution_login"]
            ]
            if len(matches) != 1:
                raise PermissionError("Issue 创建结果未知，必须先核对回执，不重发")
            issue = matches[0]
        elif (
            not relation
            or relation.get("relation") not in {"same", "none"}
            or not relation.get("evidence")
        ):
            return None
        elif relation["relation"] == "same":
            issue = next(
                (
                    item
                    for item in lookup["facts"]["issues"]
                    if item["number"] == relation.get("issue_number")
                ),
                None,
            )
            if issue is None:
                raise PermissionError("选中的相同 Issue 不在完整查重结果内")
        else:
            identity = json.loads(repository["identity_json"])
            marker = "<!-- repowatch-candidate:" + task["id"] + " -->"
            request = {
                "title": analysis.objective,
                "body": analysis.plan
                + "\n\n验收目标：\n"
                + canonical_json(analysis.criteria)
                + "\n\n扫描候选："
                + task["source_key"]
                + "\n\n"
                + marker,
                "marker": marker,
                "execution_login": identity["execution_login"],
            }
            identifier = old_intent["id"] if old_intent else str(uuid4())
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                if old_intent is None:
                    records.add(
                        "publication_intents",
                        {
                            "id": identifier,
                            "task_id": task["id"],
                            "repository_id": repository["id"],
                            "issue_id": "candidate:" + task["id"],
                            "kind": "issue",
                            "request_json": canonical_json(request),
                            "request_sha256": content_digest(request),
                            "state": "intent",
                            "created_at": services.clock(),
                        },
                    )

            def send():
                latest_issues = lookup["port"].collection(
                    lookup["prefix"] + "/issues", params={"state": "all"}
                )
                latest_issues = DEFAULT_REDACTOR.clean(
                    [item for item in latest_issues if "pull_request" not in item]
                )
                if content_digest(latest_issues) != content_digest(lookup["facts"]["issues"]):
                    raise ConflictError("创建前 Issue 集合变化，须重新查重")
                with services.store.write() as records:
                    execution_record(records, token, services.clock())
                    records.change(
                        "publication_intents", {"id": identifier}, {"state": "dispatched"}
                    )
                try:
                    return lookup["port"].write(
                        "POST",
                        lookup["prefix"] + "/issues",
                        {key: request[key] for key in ("title", "body")},
                    )
                except Exception:
                    with services.store.write() as records:
                        records.change(
                            "publication_intents", {"id": identifier}, {"state": "unknown"}
                        )
                    raise

            issue = BudgetedExecutor(
                services.budgets, services.queue, clock=services.clock
            ).execute(
                token,
                "issue-create:" + identifier,
                BudgetAmounts(active_ms=60000),
                send,
                lambda _: {},
                purpose="tool",
            )
            confirmed = lookup["port"].issue(
                repository["github_owner"], repository["github_name"], issue["number"]
            )
            if (
                confirmed.get("id") != issue.get("id")
                or marker not in (confirmed.get("body") or "")
                or confirmed.get("user", {}).get("login") != identity["execution_login"]
            ):
                raise PermissionError("Issue 创建身份未核实")
            issue = confirmed
        issue_id = str(issue["id"])
        with services.store.write() as records:
            execution_record(records, token, services.clock())
            existing = records.one("tasks", repository_id=repository["id"], issue_id=issue_id)
            target = (
                existing["id"]
                if existing
                else create_task(
                    records,
                    repository["id"],
                    "github:issue:" + issue_id,
                    issue_id,
                    "triage",
                    services.clock(),
                    owner=task["budget_owner_task_id"],
                )
            )
            if existing is None:
                records.change(
                    "tasks", {"id": target}, {"configuration_json": task["configuration_json"]}
                )
        services.tasks.observe(
            target,
            "github:issue:" + issue_id,
            content_digest(issue_input(issue)),
            issue_input(issue),
            now=services.clock(),
        )
        with services.store.write() as records:
            execution_record(records, token, services.clock())
            candidate = records.one("scan_candidates", request_task_id=task["id"])
            if candidate:
                records.change(
                    "scan_candidates",
                    {"id": candidate["id"]},
                    {"request_task_id": target, "revision": candidate["revision"] + 1},
                )
            current = required(records, "tasks", id=target)
            if (
                not current["user_excluded"]
                and not current["cancel_requested"]
                and current["state"] not in {"running", "ended", "cancelled", "blocked"}
            ):
                bump_task(
                    records, current, selected=1, state="queued", mode="triage", wait_reason=None
                )
            intent = records.one("publication_intents", task_id=task["id"], kind="issue")
            if intent:
                records.change(
                    "publication_intents",
                    {"id": intent["id"]},
                    {
                        "state": "confirmed",
                        "result_json": canonical_json(
                            {"issue_id": issue_id, "number": issue["number"], "task_id": target}
                        ),
                    },
                )
            business_event(
                records,
                "candidate_issue_confirmed",
                task["id"],
                issue_id=issue_id,
                target_task_id=target,
            )
        return target

    def decide(
        self,
        operation_id,
        task_id,
        *,
        expected_revision,
        context_digest,
        decision,
        issue_number,
        explanation,
    ):
        if (
            decision not in {"same", "none"}
            or not isinstance(explanation, str)
            or not explanation.strip()
        ):
            raise ValueError("候选查重决定无效")
        request = {
            "task_id": task_id,
            "revision": expected_revision,
            "digest": context_digest,
            "decision": decision,
            "issue_number": issue_number,
            "explanation": explanation,
        }
        with self.services.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            task = required(records, "tasks", id=task_id)
            if (
                task["revision"] != expected_revision
                or task["state"] == "running"
                or task["issue_id"] is not None
                or task["user_excluded"]
                or task["cancel_requested"]
                or task["state"] in {"blocked", "ended", "cancelled"}
            ):
                raise ConflictError("候选已变化，请刷新核对")
            decisions = records.find("triage_decisions", task_id=task_id)
            if not any(
                json.loads(item["evidence_json"]).get("lookup_digest") == context_digest
                and item["input_revision"] == task["input_revision"]
                and item["feedback_revision"] == task["feedback_revision"]
                for item in decisions
            ):
                raise ConflictError("缺少当前完整查重依据")
            records.add(
                "candidate_issue_decisions",
                {
                    "task_id": task_id,
                    "context_digest": context_digest,
                    "decision": decision,
                    "issue_number": issue_number,
                    "evidence_json": canonical_json(
                        DEFAULT_REDACTOR.clean({"explanation": explanation})
                    ),
                },
            )
            bump_task(
                records,
                task,
                state="queued",
                mode="triage",
                wait_reason=None,
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
            business_event(records, "candidate_issue_decided", task_id, decision=decision)
            return result
