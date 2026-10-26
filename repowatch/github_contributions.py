"""Trusted GitHub writes with persisted intents; no model has publishing tools."""

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from .github_accounts import GitHubError
from .issue_reuse import issue_comment, similar_issue
from .repair_origin import repair_origin
from .store import Conflict, Missing
from .task_scopes import TaskScopes, publication_body, strip_closing_references


def sha(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{40}", value):
        raise GitHubError("github_invalid_response", 502)
    return value


def remote_link(item, repository, kind):
    number = item.get("number") if isinstance(item, dict) else None
    if type(number) is not int or number < 1:
        raise GitHubError("github_invalid_response", 502)
    expected = f"https://github.com/{repository}/{kind}/{number}"
    if item.get("html_url") != expected:
        raise GitHubError("github_invalid_response", 502)
    return {"number": number, "url": expected}


class GitHubPublisher:
    def __init__(self, github):
        self.github, self.store = github, github.store
        self.scopes = TaskScopes(self.store)

    def _mutation(self, task_id, key, payload, lookup, write):
        """An unresolved POST intent is never blindly replayed, including after a crash."""
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM repair_remote_operations WHERE repair_id=? AND operation=?",
                (task_id, key),
            ).fetchone()
        if row and row["payload_hash"] != payload_hash:
            raise Conflict("repair_remote_conflict")
        if row and row["state"] == "complete":
            return json.loads(row["result"])
        found = lookup()
        if found is not None:
            self._save_operation(task_id, key, payload_hash, "complete", found)
            return found
        if row and row["state"] == "pending":
            raise Conflict("repair_publication_uncertain")
        self._save_operation(task_id, key, payload_hash, "pending")
        try:
            result = write()
        except GitHubError as exc:
            if exc.status in (401, 403, 404, 422, 429):
                self._save_operation(task_id, key, payload_hash, "rejected")
            raise
        self._save_operation(task_id, key, payload_hash, "complete", result)
        return result

    def _save_operation(self, task_id, key, payload_hash, state, result=None):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO repair_remote_operations"
                "(repair_id,operation,payload_hash,state,result,updated) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(repair_id,operation) DO UPDATE SET state=excluded.state,"
                "result=excluded.result,updated=excluded.updated",
                (task_id, key, payload_hash, state,
                 json.dumps(result) if result is not None else None, time.time()),
            )

    def publish(self, task, patch, body, *, check):
        account_id, account_version = task["account_id"], task["account_version"]
        with self.store.connection() as db:
            stored = db.execute(
                "SELECT * FROM repair_tasks WHERE id=?", (task["id"],),
            ).fetchone()
            if not stored or any(stored[key] != task[key] for key in (
                "analysis_id", "repo_id", "env_id", "revision",
            )):
                raise Conflict("repair_issue_reference_invalid")
            origin = repair_origin(db, stored)
            operations = {row["operation"]: row for row in db.execute(
                "SELECT * FROM repair_remote_operations WHERE repair_id=?", (task["id"],),
            )}
            repo = db.execute(
                "SELECT * FROM repositories WHERE id=?", (task["repo_id"],)
            ).fetchone()
        source = origin["source_issue"]
        decision = self.scopes.publication(stored, patch)
        body = publication_body(body, decision)
        binding = operations.get("source_issue")
        binding_hash = hashlib.sha256(json.dumps(origin, sort_keys=True).encode()).hexdigest()
        if source:
            # Old Issue/PR intents may already have created a duplicate. Never repurpose them.
            if "issue" in operations or ("pr" in operations and binding is None):
                raise Conflict("repair_issue_publication_conflict")
            if binding and (binding["payload_hash"] != binding_hash
                            or binding["state"] != "complete"):
                raise Conflict("repair_issue_publication_conflict")
        elif binding:
            raise Conflict("repair_issue_publication_conflict")
        if not repo or repo["kind"] != "github" or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo["name"],
        ) or repo["url"] != "https://github.com/" + repo["name"]:
            raise Conflict("github_repository_required")
        name, revision = repo["name"], sha(task["revision"])
        try:
            token, identity = self.github.validated_identity(account_id, account_version)
        except (Conflict, Missing):
            raise Conflict("repair_account_changed") from None

        def request(method, path, payload=None, *, optional=False):
            check()
            if method == "POST":
                self.scopes.validate_publication(stored, patch, decision)
            with self.store.connection() as db:
                try:
                    self.github.row(db, account_id, account_version)
                except (Conflict, Missing):
                    raise Conflict("repair_account_changed") from None
                active = db.execute(
                    "SELECT active_account_id FROM github_configuration WHERE id=1"
                ).fetchone()[0]
            if active != account_id:
                raise Conflict("repair_account_changed")
            try:
                result, _ = self.github.client.request_json(method, path, token, payload)
            except GitHubError as exc:
                if optional and exc.status == 404:
                    return None
                raise
            return result

        metadata = request("GET", f"/repos/{name}")
        if not isinstance(metadata, dict) or str(metadata.get("full_name", "")).casefold() != (
            name.casefold()
        ):
            raise GitHubError("github_invalid_response", 502)
        name = metadata["full_name"]
        base = metadata.get("default_branch")
        if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", base):
            raise GitHubError("github_invalid_response", 502)

        def source_issue():
            item = request("GET", f"/repos/{name}/issues/{source['number']}")
            if (not isinstance(item, dict) or "pull_request" in item
                    or item.get("state") not in ("open", "closed")):
                raise Conflict("repair_issue_reference_invalid")
            link = remote_link(item, name, "issues")
            if (link["number"] != source["number"]
                    or link["url"].casefold() != source["url"].casefold()):
                raise Conflict("repair_issue_reference_invalid")
            self.scopes.validate_publication(stored, patch, decision, item)
            return link

        if source:
            # Validate before the first remote write and persist a read-only source binding.
            source_issue()
            self._save_operation(task["id"], "source_issue", binding_hash, "complete", source)

        def current_upstream():
            ref = request("GET", f"/repos/{name}/git/ref/heads/{quote(base, safe='/')}")
            if sha(ref.get("object", {}).get("sha")) != revision:
                raise Conflict("repair_upstream_changed")

        current_upstream()
        target = name
        permissions = metadata.get("permissions", {})
        if not isinstance(permissions, dict) or permissions.get("push") is not True:
            if metadata.get("private") is not False or metadata.get("allow_forking") is False:
                raise Conflict("repair_fork_not_allowed")
            target = identity["login"] + "/" + name.split("/")[1]

            def validate_fork(fork):
                if (not isinstance(fork, dict) or fork.get("fork") is not True
                        or str(fork.get("full_name", "")).casefold() != target.casefold()
                        or str(fork.get("parent", {}).get("full_name", "")).casefold()
                        != name.casefold()):
                    raise Conflict("repair_fork_conflict")
                return {"repository": target}

            def find_fork():
                fork = request("GET", f"/repos/{target}", optional=True)
                return validate_fork(fork) if fork else None

            self._mutation(
                task["id"], "fork", {"repository": target}, find_fork,
                lambda: validate_fork(request("POST", f"/repos/{name}/forks", {
                    "name": name.split("/")[1], "default_branch_only": False,
                })),
            )
            fork = request("GET", f"/repos/{target}")
            if fork.get("permissions", {}).get("push") is not True:
                raise Conflict("repair_fork_pending")
        original = request("GET", f"/repos/{target}/git/commits/{revision}", optional=True)
        if original is None:
            raise Conflict("repair_fork_pending")
        tree_sha = sha(original.get("tree", {}).get("sha"))
        original_tree = request("GET", f"/repos/{target}/git/trees/{tree_sha}?recursive=1")
        if original_tree.get("truncated") is not False or not isinstance(
            original_tree.get("tree"), list,
        ):
            raise Conflict("repair_remote_scan_limit")
        original_entries = {
            entry["path"]: entry for entry in original_tree["tree"]
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        entries = []
        for edit in patch["edits"]:
            existing = original_entries.get(edit["path"])
            if edit["original_sha256"]:
                if (not existing or existing.get("type") != "blob"
                        or existing.get("sha") != edit.get("original_git_blob")
                        or existing.get("mode") not in ("100644", "100755")):
                    raise Conflict("repair_source_changed")
                mode = existing["mode"]
            else:
                if existing:
                    raise Conflict("repair_remote_conflict")
                mode = "100644"
            blob = request("POST", f"/repos/{target}/git/blobs", {
                "content": base64.b64encode(edit["content"].encode()).decode(),
                "encoding": "base64",
            })
            entries.append({"path": edit["path"], "mode": mode,
                            "type": "blob", "sha": sha(blob.get("sha"))})
        tree = request("POST", f"/repos/{target}/git/trees", {
            "base_tree": tree_sha, "tree": entries,
        })
        finding_title = strip_closing_references(json.loads(task["context"])["finding"]["title"])
        title = "修复: " + finding_title[:100]
        date = datetime.fromtimestamp(task["created"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Fixed identity, parent, tree and timestamp make retries content-addressed.
        author = {
            "name": identity["login"],
            "email": f"{account_id}+{identity['login']}@users.noreply.github.com",
            "date": date,
        }
        commit = request("POST", f"/repos/{target}/git/commits", {
            "message": title + "\n\nRepoWatch " + task["id"],
            "tree": sha(tree.get("sha")), "parents": [revision],
            "author": author, "committer": author,
        })
        commit_sha = sha(commit.get("sha"))
        branch = "repowatch/" + task["id"].replace("_", "-")

        def find_branch():
            ref = request("GET", f"/repos/{target}/git/ref/heads/{branch}", optional=True)
            if ref is None:
                return None
            if sha(ref.get("object", {}).get("sha")) != commit_sha:
                raise Conflict("repair_remote_conflict")
            return {"repository": target, "branch": branch, "commit": commit_sha}

        def create_branch():
            ref = request("POST", f"/repos/{target}/git/refs", {
                "ref": "refs/heads/" + branch, "sha": commit_sha,
            })
            if sha(ref.get("object", {}).get("sha")) != commit_sha:
                raise GitHubError("github_invalid_response", 502)
            return {"repository": target, "branch": branch, "commit": commit_sha}

        branch_result = self._mutation(
            task["id"], "branch", {"target": target, "branch": branch, "sha": commit_sha},
            find_branch, create_branch,
        )
        # Even a completed intent must not publish a PR from an externally changed branch.
        find_branch()
        current_upstream()
        marker = "<!-- repowatch:" + task["id"] + " -->"
        reference_mode = decision["mode"]

        def find_marked(kind, extra=None):
            found = []
            for page in range(1, 21):
                query = urlencode({"state": "all", "per_page": 100, "page": page, **(extra or {})})
                items = request("GET", f"/repos/{name}/{kind}?{query}")
                if not isinstance(items, list):
                    raise GitHubError("github_invalid_response", 502)
                for item in items:
                    if not isinstance(item, dict) or kind == "issues" and "pull_request" in item:
                        continue
                    if marker in str(item.get("body") or ""):
                        if item.get("user", {}).get("id") != account_id:
                            raise Conflict("repair_remote_conflict")
                        found.append(item)
                if len(items) < 100:
                    break
            else:
                raise Conflict("repair_remote_scan_limit")
            if len(found) > 1:
                raise Conflict("repair_remote_conflict")
            if not found:
                return None
            if kind == "pulls" and (
                found[0].get("head", {}).get("sha") != commit_sha
                or found[0].get("base", {}).get("ref") != base
            ):
                raise Conflict("repair_remote_conflict")
            if kind == "pulls" and (
                f"\n\n{reference_mode.title()} #{issue['number']}\n\n" + marker
                not in str(found[0].get("body") or "")
            ):
                raise Conflict("repair_issue_publication_conflict")
            return remote_link(found[0], name, "issues" if kind == "issues" else "pull")

        reused = False
        if source:
            issue = source_issue()
        elif "issue_target" in operations or ("issue" not in operations and "pr" not in operations):
            # Freeze the read-only selection before any Issue/comment POST. Retries
            # retain this binding even when the remote list or ranking later changes.
            selection = operations.get("issue_target")
            target_payload = {"repository": name, "finding_id": task["finding_id"],
                              "policy": "source_and_symptom_v1"}
            target_hash = hashlib.sha256(
                json.dumps(target_payload, sort_keys=True).encode(),
            ).hexdigest()
            if selection:
                if selection["state"] != "complete" or selection["payload_hash"] != target_hash:
                    raise Conflict("repair_issue_publication_conflict")
                selected = json.loads(selection["result"])
            if not selection or (selected["issue"] is None and "issue" not in operations
                                 and "pr" not in operations):
                candidate = similar_issue(request, name, json.loads(task["context"])["finding"])
                selected = ({"issue": remote_link(candidate["item"], name, "issues"),
                             "reason": candidate["reason"], "paths": candidate["paths"],
                             "score": candidate["score"]} if candidate else {"issue": None})
                self._save_operation(task["id"], "issue_target", target_hash, "complete", selected)
            if selected["issue"]:
                if "issue" in operations:
                    raise Conflict("repair_issue_publication_conflict")
                # Validate the frozen target on every attempt; never trust a stored URL
                # or a PR returned by the Issues API as an Issue binding.
                item = request("GET", f"/repos/{name}/issues/{selected['issue']['number']}")
                if (not isinstance(item, dict) or "pull_request" in item
                        or item.get("state") not in ("open", "closed")
                        or remote_link(item, name, "issues") != selected["issue"]):
                    raise Conflict("repair_issue_reference_invalid")
                issue, reused = selected["issue"], True
            else:
                issue = None
        else:
            issue = None
        if not source and not reused:
            issue_payload = {"title": "[RepoWatch] " + finding_title[:100],
                             "body": body + "\n\n" + marker}
            issue = self._mutation(
                task["id"], "issue", issue_payload, lambda: find_marked("issues"),
                lambda: remote_link(request("POST", f"/repos/{name}/issues", issue_payload),
                                    name, "issues"),
            )
        current_upstream()
        if reused:
            reference_mode = "refs"
            comment_body = ("## RepoWatch 补充发现与修复\n\n" + body
                            + "\n\n此补充关联相似问题；原 Issue 的状态保持不变。\n\n" + marker)
            lookup, write = issue_comment(request, name, issue["number"], marker,
                                          account_id, comment_body)
            self._mutation(task["id"], "issue_comment",
                           {"issue": issue, "body": comment_body}, lookup, write)
            body += "\n\n本次复用相似 Issue，仅引用原编号，不自动关闭原 Issue。"
        self.scopes.validate_publication(stored, patch, decision)
        head = target.split("/")[0] + ":" + branch
        reference = f"\n\n{reference_mode.title()} #{issue['number']}\n\n" + marker
        pr_payload = {"title": title, "head": head, "base": base, "body": body + reference}
        pr = self._mutation(
            task["id"], "pr", pr_payload,
            lambda: find_marked("pulls", {"head": head}),
            lambda: remote_link(request("POST", f"/repos/{name}/pulls", pr_payload), name, "pull"),
        )
        return {**branch_result, "issue": issue, "pr": pr,
                "account_id": account_id, "account_login": identity["login"],
                "scope_assessment": decision, "issue_reused": reused}
