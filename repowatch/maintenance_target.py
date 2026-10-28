"""Fixed-repository maintenance authority and replay-safe GitHub receipts."""

import ast
import base64
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import quote

from .github_accounts import GitHubError
from .github_contributions import GitHubPublisher, remote_link, sha
from .issue_reuse import issue_comment
from .source_safety import source_problem
from .store import Conflict
from .task_scopes import strip_closing_references

REPOSITORY = "Gwex323/RepoWatch"
LOGIN = "Gwex323"


class MaintenanceTarget(GitHubPublisher):
    operation_table = "maintenance_remote_operations"
    operation_owner = "task_id"

    def authority(self, account_id, account_version, repository_id=None):
        token, identity = self.github.validated_identity(account_id, account_version)
        with self.store.connection() as db:
            active = db.execute(
                "SELECT active_account_id FROM github_configuration WHERE id=1"
            ).fetchone()[0]
        if identity["login"] != LOGIN or active != account_id:
            raise Conflict("maintenance_identity_required")
        metadata, _ = self.github.client.request_json("GET", f"/repos/{REPOSITORY}", token)
        if (
            not isinstance(metadata, dict)
            or metadata.get("full_name") != REPOSITORY
            or type(metadata.get("id")) is not int
            or repository_id is not None
            and metadata["id"] != repository_id
            or metadata.get("permissions", {}).get("push") is not True
        ):
            raise Conflict("maintenance_repository_permission_required")
        branch = metadata.get("default_branch")
        if (
            not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", branch)
            or ".." in branch
            or branch.startswith(("-", "/"))
        ):
            raise Conflict("maintenance_default_branch_invalid")
        return token, identity, metadata

    def session(self, binding, check):
        token, identity, metadata = self.authority(**binding)

        def request(method, path, payload=None, *, optional=False):
            check()
            with self.store.connection() as db:
                self.github.row(db, binding["account_id"], binding["account_version"])
                active = db.execute(
                    "SELECT active_account_id FROM github_configuration WHERE id=1"
                ).fetchone()[0]
            if active != binding["account_id"]:
                raise Conflict("maintenance_identity_required")
            if method != "GET":
                self.authority(**binding)
                check()
            try:
                result, _ = self.github.client.request_json(method, path, token, payload)
                return result
            except GitHubError as exc:
                if optional and exc.status == 404:
                    return None
                raise

        return request, token, identity, metadata

    @staticmethod
    def items(request, kind):
        result = []
        for page in range(1, 21):
            rows = request("GET", f"/repos/{REPOSITORY}/{kind}?state=all&per_page=100&page={page}")
            if not isinstance(rows, list):
                raise Conflict("maintenance_remote_response_invalid")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Conflict("maintenance_remote_scan_limit")

    def evidence(self, binding, facts, check):
        request, _, _, metadata = self.session(binding, check)
        branch = metadata["default_branch"]
        ref = request("GET", f"/repos/{REPOSITORY}/git/ref/heads/{quote(branch, safe='/')}")
        revision = sha(ref.get("object", {}).get("sha"))
        tree = request("GET", f"/repos/{REPOSITORY}/git/trees/{revision}?recursive=1")
        if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise Conflict("maintenance_source_scan_limit")
        entries = {
            row["path"]: row
            for row in tree["tree"]
            if row.get("type") == "blob"
            and row.get("mode") in ("100644", "100755")
            and (
                row.get("path", "").startswith("repowatch/")
                and row["path"].endswith(".py")
                or row.get("path", "").startswith("frontend/")
                and row["path"].endswith((".ts", ".tsx"))
            )
        }
        hints = " ".join(str(v) for v in facts.values()).lower()
        names = sorted(
            entries,
            key=lambda name: (
                not any(
                    word in hints
                    for word in name.rsplit("/", 1)[-1].split(".")[0].split("_")
                    if len(word) > 3
                ),
                name,
            ),
        )
        sources, starts, total, downloaded = {}, {}, 0, 0
        for name in names:
            check()
            if len(sources) >= 10 or total >= 75000:
                break
            entry = entries[name]
            if not 0 < entry.get("size", 1000000) <= 512000:
                continue
            downloaded += entry["size"]
            if downloaded > 2000000:
                break
            blob = request("GET", f"/repos/{REPOSITORY}/git/blobs/{sha(entry['sha'])}")
            try:
                raw = base64.b64decode(blob["content"], validate=False)
                if (
                    len(raw) != entry["size"]
                    or len(raw) > 512000
                    or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                    != entry["sha"]
                ):
                    raise ValueError
                text = raw.decode("utf-8")
            except (KeyError, ValueError, UnicodeError):
                raise Conflict("maintenance_source_invalid") from None
            if source_problem(text, name):
                continue
            start = 1
            if len(text) > 16000:
                try:
                    nodes = [
                        node
                        for node in ast.walk(ast.parse(text))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.end_lineno - node.lineno < 250
                    ]
                except SyntaxError:
                    nodes = []
                words = set(re.findall(r"[a-z]{4,}", hints))
                lines = text.splitlines(keepends=True)
                chunks = [
                    (
                        sum(
                            word in "".join(lines[node.lineno - 1 : node.end_lineno]).lower()
                            for word in words
                        ),
                        node.lineno,
                        "".join(lines[node.lineno - 1 : node.end_lineno]),
                    )
                    for node in nodes
                ]
                chunks = [chunk for chunk in chunks if len(chunk[2]) <= 16000 and chunk[0] > 0]
                if not chunks:
                    continue
                _, start, text = max(chunks, key=lambda chunk: chunk[0])
            sources[name] = text
            starts[name] = start
            total += len(text)
        return {
            "repository": REPOSITORY,
            "base": revision,
            "default_branch": branch,
            "files": sorted(entries)[:600],
            "sources": sources,
            "source_starts": starts,
            "facts": facts,
        }

    @staticmethod
    def matches(item, fingerprint):
        return (
            isinstance(item, dict)
            and item.get("user", {}).get("login") == LOGIN
            and f"<!-- repowatch-maintenance-root:{fingerprint} -->" in (item.get("body") or "")
        )

    @classmethod
    def root_match(cls, item, task, diagnosis):
        if cls.matches(item, task["fingerprint"]):
            return True
        body = item.get("body") or ""
        # Human-written work needs implementation, cause and acceptance evidence together.
        return (
            diagnosis["path"] in body
            and diagnosis["symbol"] in body
            and (
                task["code"] in body
                and diagnosis["invariant"] in body
                or diagnosis["root_cause"] in body
                and diagnosis["expected"] in body
            )
        )

    @classmethod
    def candidates(cls, items, task, diagnosis):
        matches = [item for item in items if cls.root_match(item, task, diagnosis)]
        uncertain = [
            item
            for item in items
            if item not in matches and diagnosis["path"] in (item.get("body") or "")
        ]
        if uncertain:
            raise Conflict("maintenance_related_work_requires_review")
        return matches

    def ensure_issue(self, task, diagnosis, check):
        request, _, _, _ = self.session(task["binding"], check)
        marker = f"<!-- repowatch-maintenance-task:{task['id']} -->"
        root = f"<!-- repowatch-maintenance-root:{task['fingerprint']} -->"
        body = strip_closing_references(
            f"## 问题\n{diagnosis['root_cause']}\n\n## 影响范围\n"
            f"`{REPOSITORY}` · `{diagnosis['path']}` · `{diagnosis['symbol']}`\n"
            f"故障：`{task['code']}`；约束：`{diagnosis['invariant']}`\n\n"
            f"## 预期行为\n{diagnosis['expected']}\n\n## 证据\n{diagnosis['observed']}\n"
            f"基础提交：`{task['base']}`；故障记录：`{task['occurrence_id']}`\n"
            f"```python\n{diagnosis['quote']}\n```\n\n{root}\n{marker}"
        )
        issues = [item for item in self.items(request, "issues") if "pull_request" not in item]
        matches = self.candidates(issues, task, diagnosis)
        pulls = self.candidates(self.items(request, "pulls"), task, diagnosis)
        if len(matches) > 1:
            raise Conflict("maintenance_multiple_related_issues")
        if len(pulls) > 1 or pulls and not matches:
            raise Conflict("maintenance_related_work_requires_review")
        # Persist selection before commenting; a title-only match has no root-cause proof.
        issue = self._mutation(
            task["id"],
            "issue",
            {"fingerprint": task["fingerprint"]},
            lambda: remote_link(matches[0], REPOSITORY, "issues") if matches else None,
            lambda: remote_link(
                request(
                    "POST",
                    f"/repos/{REPOSITORY}/issues",
                    {"title": "自身维护：" + diagnosis["title"], "body": body},
                ),
                REPOSITORY,
                "issues",
            ),
        )
        item = request("GET", f"/repos/{REPOSITORY}/issues/{issue['number']}")
        if (
            not self.root_match(item, task, diagnosis)
            or "pull_request" in item
            or remote_link(item, REPOSITORY, "issues") != issue
        ):
            raise Conflict("maintenance_issue_changed")
        if marker not in (item.get("body") or ""):
            lookup, write = issue_comment(
                request, REPOSITORY, issue["number"], marker, task["binding"]["account_id"], body
            )
            self._mutation(
                task["id"], "issue_evidence", {"issue": issue, "body": body}, lookup, write
            )
        related = None
        if pulls:
            item = pulls[0]
            if (
                f"#{issue['number']}" not in (item.get("body") or "")
                or item.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
                or not item.get("head", {}).get("ref", "").startswith("feature/")
            ):
                raise Conflict("maintenance_related_pr_invalid")
            if item.get("state") == "open" or item.get("merged_at"):
                related = {
                    **remote_link(item, REPOSITORY, "pull"),
                    "merged": bool(item.get("merged_at")),
                }
                lookup, write = issue_comment(
                    request,
                    REPOSITORY,
                    related["number"],
                    marker,
                    task["binding"]["account_id"],
                    body,
                    kind="pull",
                )
                self._mutation(
                    task["id"], "pr_evidence", {"pr": related, "body": body}, lookup, write
                )
        return issue, related

    def publish(self, task, patch, verification, review, check):
        request, _, identity, metadata = self.session(task["binding"], check)
        issue = request("GET", f"/repos/{REPOSITORY}/issues/{task['issue']['number']}")
        if not self.root_match(issue, task, task["diagnosis"]):
            raise Conflict("maintenance_issue_changed")
        if metadata["default_branch"] != task["default_branch"]:
            raise Conflict("maintenance_upstream_changed")
        ref = request(
            "GET", f"/repos/{REPOSITORY}/git/ref/heads/{quote(task['default_branch'], safe='/')}"
        )
        if sha(ref.get("object", {}).get("sha")) != task["base"]:
            raise Conflict("maintenance_upstream_changed")
        commit = request("GET", f"/repos/{REPOSITORY}/git/commits/{task['base']}")
        tree_sha = sha(commit.get("tree", {}).get("sha"))
        tree = request("GET", f"/repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated") is not False:
            raise Conflict("maintenance_source_scan_limit")
        entries = {entry["path"]: entry for entry in tree.get("tree", [])}
        changes = []
        for edit in patch["edits"]:
            check()
            entry = entries.get(edit["path"])
            if edit["original_sha256"]:
                if (
                    not entry
                    or entry.get("type") != "blob"
                    or entry.get("mode") not in ("100644", "100755")
                ):
                    raise Conflict("maintenance_source_changed")
                blob = request("GET", f"/repos/{REPOSITORY}/git/blobs/{sha(entry['sha'])}")
                raw = base64.b64decode(blob["content"])
                if hashlib.sha256(raw).hexdigest() != edit["original_sha256"]:
                    raise Conflict("maintenance_source_changed")
            elif entry:
                raise Conflict("maintenance_source_changed")
            blob = request(
                "POST",
                f"/repos/{REPOSITORY}/git/blobs",
                {
                    "content": base64.b64encode(edit["content"].encode()).decode(),
                    "encoding": "base64",
                },
            )
            changes.append(
                {
                    "path": edit["path"],
                    "mode": entry["mode"] if entry else "100644",
                    "type": "blob",
                    "sha": sha(blob.get("sha")),
                }
            )
        tree = request(
            "POST", f"/repos/{REPOSITORY}/git/trees", {"base_tree": tree_sha, "tree": changes}
        )
        title = "自身维护：" + task["diagnosis"]["title"]
        author = {
            "name": LOGIN,
            "email": f"{identity['id']}+{LOGIN}@users.noreply.github.com",
            "date": datetime.fromtimestamp(task["created"], timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        commit = request(
            "POST",
            f"/repos/{REPOSITORY}/git/commits",
            {
                "message": title + f"\n\nRefs #{task['issue']['number']}\n{task['id']}",
                "tree": sha(tree.get("sha")),
                "parents": [task["base"]],
                "author": author,
                "committer": author,
            },
        )
        commit_sha = sha(commit.get("sha"))

        def branch_lookup():
            result = request(
                "GET", f"/repos/{REPOSITORY}/git/ref/heads/{task['branch']}", optional=True
            )
            if result is None:
                return None
            if sha(result.get("object", {}).get("sha")) != commit_sha:
                raise Conflict("maintenance_branch_conflict")
            return {"branch": task["branch"], "commit": commit_sha}

        def branch_write():
            result = request(
                "POST",
                f"/repos/{REPOSITORY}/git/refs",
                {"ref": "refs/heads/" + task["branch"], "sha": commit_sha},
            )
            if sha(result.get("object", {}).get("sha")) != commit_sha:
                raise Conflict("maintenance_branch_conflict")
            return {"branch": task["branch"], "commit": commit_sha}

        self._mutation(
            task["id"],
            "branch",
            {"branch": task["branch"], "commit": commit_sha},
            branch_lookup,
            branch_write,
        )
        if branch_lookup() is None:
            raise Conflict("maintenance_branch_conflict")
        marker = f"<!-- repowatch-maintenance-task:{task['id']} -->"
        root = f"<!-- repowatch-maintenance-root:{task['fingerprint']} -->"
        body = strip_closing_references(
            f"## 概述\n{task['diagnosis']['root_cause']}\n\n"
            f"Refs #{task['issue']['number']}\n故障 `{task['occurrence_id']}`；"
            f"维护任务 `{task['id']}`\n\n"
            f"## 变更内容\n{patch['summary']}\n\n## 设计方案/说明\n"
            f"基础 `{task['base']}`，隔离分支 `{task['branch']}`。\n"
            f"仅修复 `{task['diagnosis']['path']}` 的 `{task['diagnosis']['invariant']}`。\n"
            "合并由所有者操作，不自动部署，不将 PR 创建或合并视为原故障恢复。\n\n"
            f"## 验证/验收\n补丁验证：{'通过' if verification['passed'] else '未通过'}\n"
            f"回归命令：`{patch['regression_command']}`\n\n## review记录\n"
            f"独立审查通过：{review['summary']}\n"
            f"补丁摘要 `{task['approved_digest']}`\n\n{root}\n{marker}"
        )
        payload = {
            "title": title,
            "body": body,
            "head": task["branch"],
            "base": task["default_branch"],
        }

        def validate_pr(item):
            if (
                not self.matches(item, task["fingerprint"])
                or item.get("head", {}).get("sha") != commit_sha
                or item.get("base", {}).get("ref") != task["default_branch"]
                or item.get("head", {}).get("ref") != task["branch"]
                or item.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
                or item.get("body") != body
            ):
                raise Conflict("maintenance_pr_changed")
            return remote_link(item, REPOSITORY, "pull")

        def pr_lookup():
            matches = [
                item for item in self.items(request, "pulls") if marker in (item.get("body") or "")
            ]
            if len(matches) > 1:
                raise Conflict("maintenance_multiple_related_prs")
            return validate_pr(matches[0]) if matches else None

        def pr_write():
            if branch_lookup() is None:
                raise Conflict("maintenance_branch_conflict")
            return validate_pr(request("POST", f"/repos/{REPOSITORY}/pulls", payload))

        return self._mutation(
            task["id"],
            "pr",
            payload,
            pr_lookup,
            pr_write,
        )
