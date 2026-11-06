"""唯一持久发布意图；回执未知先查询，不能凭标题认领远端对象。"""

import json
import re
from uuid import uuid4

import httpx

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.application.acceptance import acceptance_view
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.github_reads import issue_input
from repowatch.application.issue_context import IssueContexts
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.domain.triage import TriageAnalysis, classify_analysis, host_triage_gate
from repowatch.security import DEFAULT_REDACTOR


class GitHubPublisher(GitHubReadAdapter):
    def write(self, method, path, body):
        secret = self.vault.read(self.reference).reveal()
        try:
            with httpx.Client(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=30
            ) as client:
                response = client.request(
                    method,
                    self.base_url + path,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": "Bearer " + secret,
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json=body,
                )
                if not response.is_success:
                    raise PermissionError("GitHub 写入未确认，须读取对账")
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise PermissionError("GitHub 写入结果未知，须读取对账") from None


def pr_body(issue_number, evidence, marker, source):
    review = evidence.get("review", {})
    details = {
        "base_probe": {
            key: evidence.get("base_probe", {}).get(key) for key in ("exit_code", "log_sha256")
        },
        "candidate_probe": {
            key: evidence.get("candidate_probe", {}).get(key) for key in ("exit_code", "log_sha256")
        },
        "artifact_ids": evidence.get("artifact_ids", []),
        "review_run_id": evidence.get("review_run_id"),
        "review_passed": evidence.get("review_passed"),
        "limitations": ["PR 专属 CI 尚未执行", "证据归档位于本地"],
    }
    source = re.sub(
        r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+((?:[\w.-]+/[\w.-]+)?#\d+|https?://\S+)",
        "[外部关闭指令已移除]",
        source,
    )
    return (
        "## 概述\n\n处理已确认的 Issue #"
        + str(issue_number)
        + "。\n\n## 变更内容\n\n"
        + source
        + "\n\n## 设计方案/说明\n\n变更绑定冻结候选与当前验收规范。"
        + "\n\n## 验证/验收\n\n修改前后探针、回归与独立审查证据：\n\n```json\n"
        + canonical_json(DEFAULT_REDACTOR.clean(details))
        + "\n```\n\nPR 专属 CI 尚未执行。"
        + "\n\n## review记录\n\n独立只读审查通过；结论摘要："
        + ("通过" if review.get("passed") else "以归档结果为准")
        + "。\n\n"
        + "Closes #"
        + str(issue_number)
        + "\n\n"
        + marker
    )


class Publisher:
    def __init__(self, services, evidence):
        self.services, self.evidence = services, evidence

    def reconcile_pending(self, token, intent):
        services = self.services
        request = json.loads(intent["request_json"])
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
        if intent["task_id"] != task["id"] or intent["issue_id"] != task["issue_id"]:
            raise PermissionError("回执意图不属于当前 Issue")
        if repository["github_id"] != request["upstream_id"]:
            raise PermissionError("回执上游身份不匹配")
        port = GitHubPublisher(
            vault=services.vault, credential_reference=repository["credential_reference"]
        )
        publication = json.loads(repository["publication_json"])
        if publication["github_id"] != request["publication_id"]:
            raise PermissionError("回执发布仓库身份不匹配")
        result = BudgetedExecutor(services.budgets, services.queue, clock=services.clock).execute(
            token,
            "publication-read-reconcile:" + uuid4().hex,
            BudgetAmounts(active_ms=180000),
            lambda: self.reconcile(
                token,
                intent,
                port,
                port._path(repository["github_owner"], repository["github_name"]),
                publication,
                task,
                finish=False,
            ),
            lambda _: {},
            purpose="tool",
        )
        services.queue.complete(
            token, now=services.clock(), state="waiting", wait_reason="pr_review"
        )
        return result

    def publish(self, token, session_id, prior_context):
        services = self.services
        session, frozen = self.evidence.require_current(token, session_id)
        if session["state"] != "verified":
            raise PermissionError("候选尚未通过完整验证与独审")
        if not services.repositories.require_ready(token, now=services.clock()):
            return {"state": "waiting", "reason": "scan"}
        current = IssueContexts(services).read(token)
        if current["digest"] != prior_context["digest"]:
            raise PermissionError("发布前 Issue、占用或 PR 发生变化，须重新分诊")
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
            binding = records.one("managed_pull_requests", task_id=task["id"])
            if binding and binding["state"] != "open":
                raise PermissionError("受管 PR 已结束，不能重建或替换")
            inputs = json.loads(task["input_json"])
            publication = json.loads(repository["publication_json"])
            intent = records.one("publication_intents", task_id=task["id"], kind="pull_request")
            acceptance = acceptance_view(records, task)
            triages = [
                item
                for item in records.find("triage_decisions", task_id=task["id"])
                if item["input_revision"] == task["input_revision"]
                and item["feedback_revision"] == task["feedback_revision"]
                and item["admission_revision"] == repository["admission_revision"]
            ]
            candidates = records.find("scan_candidates", request_task_id=task["id"])
            source = "来源：" + task["source_key"]
            if candidates:
                source += "；扫描候选：" + ", ".join(item["id"] for item in candidates)
        if not triages:
            raise PermissionError("发布缺少当前版本分诊")
        gate = host_triage_gate(repository, task, issue_input(current["facts"]["issue"]))
        analysis = TriageAnalysis.model_validate(
            json.loads(triages[-1]["evidence_json"])["analysis"]
        )
        decision = gate or classify_analysis(
            analysis, acceptance=acceptance, remote_context=current
        )
        merged_evidence = json.loads(session["evidence_json"]).get("external_merge")
        verified_remaining = bool(
            decision.reason_code == "external_merge_verification"
            and merged_evidence
            and merged_evidence.get("pr_id") == decision.evidence["pr_id"]
            and merged_evidence.get("context_digest") == current["digest"]
            and merged_evidence.get("base_sha") == frozen["base_sha"]
            and merged_evidence.get("target_still_fails") is True
        )
        if decision.result != "auto_repair" or (
            decision.reason_code == "external_merge_verification" and not verified_remaining
        ):
            services.queue.complete(
                token, now=services.clock(), state="waiting", wait_reason=decision.reason_code
            )
            return {"state": "waiting", "reason": decision.reason_code}
        port = GitHubPublisher(
            vault=services.vault, credential_reference=repository["credential_reference"]
        )
        branch = "repowatch/issue-" + str(inputs["number"]) + "-" + task["id"][:12]
        upstream_path = port._path(repository["github_owner"], repository["github_name"])
        port._path(publication["owner"], publication["name"])
        if intent is None:
            identifier = str(uuid4())
            marker = "<!-- repowatch-publication:" + identifier + " -->"
            evidence = json.loads(session["evidence_json"])
            request = {
                "base": repository["target_branch"],
                "head": publication["owner"] + ":" + branch,
                "head_sha": frozen["sha"],
                "base_sha": frozen["base_sha"],
                "upstream_id": repository["github_id"],
                "publication_id": publication["github_id"],
                "execution_login": json.loads(repository["identity_json"])["execution_login"],
                "branch": branch,
                "marker": marker,
                "title": "RepoWatch: fix issue #" + str(inputs["number"]),
                "body": pr_body(inputs["number"], evidence, marker, source),
            }
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.add(
                    "publication_intents",
                    {
                        "id": identifier,
                        "task_id": task["id"],
                        "repository_id": repository["id"],
                        "issue_id": task["issue_id"],
                        "kind": "pull_request",
                        "request_json": canonical_json(request),
                        "request_sha256": content_digest(request),
                        "state": "intent",
                        "created_at": services.clock(),
                    },
                )
            intent = {"id": identifier, "state": "intent", "request_json": canonical_json(request)}
        else:
            request = json.loads(intent["request_json"])
            if request["head_sha"] != frozen["sha"]:
                if intent["state"] != "confirmed" or not binding:
                    raise PermissionError("已有发布意图属于其他候选，必须先对账")
                previous_receipt = json.loads(intent["result_json"])
                previous_pull = port.pull_request(
                    repository["github_owner"],
                    repository["github_name"],
                    previous_receipt["number"],
                )
                if (
                    str(previous_pull["id"]) != binding["pr_id"]
                    or previous_pull["state"] != "open"
                    or previous_pull.get("merged_at")
                    or previous_pull["head"]["sha"] != request["head_sha"]
                ):
                    raise PermissionError("原 PR 已关闭或远端 head 变化，先整合并重新验证")
                expected_old = request["head_sha"]
                objects = self.evidence.workspaces.backend.objects
                ancestor = (
                    objects.run(
                        objects.repository(repository["id"]),
                        "merge-base",
                        expected_old,
                        frozen["sha"],
                    )
                    .decode()
                    .strip()
                )
                if ancestor != expected_old:
                    raise PermissionError("返修候选未保留远端原 head")
                history = json.loads(intent["result_json"])
                with services.store.write() as records:
                    revisions = records.find("publication_revisions", intent_id=intent["id"])
                    records.add(
                        "publication_revisions",
                        {
                            "intent_id": intent["id"],
                            "revision": len(revisions) + 1,
                            "request_json": intent["request_json"],
                            "result_json": canonical_json(history),
                        },
                    )
                    request = {
                        **request,
                        "head_sha": frozen["sha"],
                        "base_sha": frozen["base_sha"],
                        "expected_old_sha": expected_old,
                        "pr_number": previous_receipt["number"],
                        "body": pr_body(
                            inputs["number"],
                            json.loads(session["evidence_json"]),
                            request["marker"],
                            source,
                        ),
                    }
                    records.change(
                        "publication_intents",
                        {"id": intent["id"]},
                        {
                            "state": "intent",
                            "request_json": canonical_json(request),
                            "request_sha256": content_digest(request),
                        },
                    )
                intent = {
                    **dict(intent),
                    "state": "intent",
                    "request_json": canonical_json(request),
                }
        executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)
        if intent["state"] in {"dispatched", "unknown", "confirmed"}:
            result = executor.execute(
                token,
                "publication-reconcile:" + uuid4().hex,
                BudgetAmounts(active_ms=180000),
                lambda: self.reconcile(
                    token, intent, port, upstream_path, publication, task, finish=False
                ),
                lambda _: {},
                purpose="tool",
            )
            services.queue.complete(
                token, now=services.clock(), state="waiting", wait_reason="pr_review"
            )
            return result

        def send():
            self.evidence.require_current(token, session_id)
            upstream = port.repository(repository["github_owner"], repository["github_name"])
            target = port.repository(publication["owner"], publication["name"])
            if (
                upstream["github_id"] != request["upstream_id"]
                or target["github_id"] != request["publication_id"]
            ):
                raise PermissionError("发布身份发生变化")
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.change("publication_intents", {"id": intent["id"]}, {"state": "dispatched"})
            try:
                objects = self.evidence.workspaces.backend.objects
                bare = objects.repository(repository["id"])
                refs = objects.run(
                    bare,
                    "ls-remote",
                    "https://github.com/"
                    + publication["owner"]
                    + "/"
                    + publication["name"]
                    + ".git",
                    "refs/heads/" + branch,
                    secret=services.vault.read(repository["credential_reference"]),
                )
                observed = refs.decode().split()[0] if refs.strip() else ""
                if observed != request.get("expected_old_sha", ""):
                    raise PermissionError("受管分支已存在，禁止覆盖外部内容")
                objects.run(
                    bare,
                    "push",
                    "--force-with-lease=refs/heads/"
                    + branch
                    + ":"
                    + request.get("expected_old_sha", ""),
                    "https://github.com/"
                    + publication["owner"]
                    + "/"
                    + publication["name"]
                    + ".git",
                    frozen["sha"] + ":refs/heads/" + branch,
                    secret=services.vault.read(repository["credential_reference"]),
                )
                if request.get("pr_number"):
                    port.write(
                        "PATCH",
                        upstream_path + "/pulls/" + str(request["pr_number"]),
                        {"body": request["body"]},
                    )
                else:
                    port.write(
                        "POST",
                        upstream_path + "/pulls",
                        {key: request[key] for key in ("title", "body", "base", "head")},
                    )
            except Exception:
                with services.store.write() as records:
                    records.change(
                        "publication_intents", {"id": intent["id"]}, {"state": "unknown"}
                    )
                raise
            return self.reconcile(
                token, intent, port, upstream_path, publication, task, finish=False
            )

        result = executor.execute(
            token,
            "publish:" + intent["id"] + ":" + content_digest(request),
            BudgetAmounts(active_ms=600000),
            send,
            lambda _: {},
            purpose="publish",
        )
        services.queue.complete(
            token, now=services.clock(), state="waiting", wait_reason="pr_review"
        )
        return result

    def reconcile(self, token, intent, port, upstream_path, publication, task, *, finish=True):
        request = json.loads(intent["request_json"])
        matches = []
        for pull in port.collection(
            upstream_path + "/pulls", params={"state": "all", "head": request["head"]}
        ):
            if request["marker"] not in (pull.get("body") or ""):
                continue
            if (
                pull.get("user", {}).get("login") != request["execution_login"]
                or pull.get("head", {}).get("ref") != request["branch"]
                or pull.get("head", {}).get("sha") != request["head_sha"]
                or str(pull.get("head", {}).get("repo", {}).get("id")) != request["publication_id"]
                or pull.get("base", {}).get("ref") != request["base"]
                or str(pull.get("base", {}).get("repo", {}).get("id")) != request["upstream_id"]
            ):
                raise PermissionError("发布回执版本不匹配")
            matches.append(pull)
        if len(matches) != 1:
            raise PermissionError("发布结果尚未唯一确认，不重发创建")
        pull = matches[0]
        self.services.tasks.bind_managed_pr(
            task["id"],
            request["upstream_id"],
            str(pull["id"]),
            state="merged" if pull.get("merged_at") else pull["state"],
        )
        result = {
            "state": "waiting",
            "reason": "pr_review",
            "pr_id": str(pull["id"]),
            "number": pull["number"],
            "url": pull["html_url"],
            "sha": request["head_sha"],
        }
        with self.services.store.write() as records:
            execution_record(records, token, self.services.clock())
            records.change(
                "publication_intents",
                {"id": intent["id"]},
                {"state": "confirmed", "result_json": canonical_json(result)},
            )
            business_event(records, "publication_confirmed", task["id"], **result)
        if finish:
            self.services.queue.complete(
                token, now=self.services.clock(), state="waiting", wait_reason="pr_review"
            )
        return result
