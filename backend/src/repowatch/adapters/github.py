"""有界 GitHub REST GET 适配，凭据仅用于宿主 HTTP 请求。"""

import json
import re
from urllib.parse import quote

import httpx

from repowatch.application.github_ports import GitHubReadError, ReadPage
from repowatch.domain.providers import validate_url
from repowatch.domain.repositories import RepositoryRequest


class GitHubReadAdapter:
    def __init__(
        self,
        *,
        vault=None,
        credential_reference=None,
        base_url="https://api.github.com",
        transport=None,
    ):
        self.base_url = validate_url(base_url)
        self.vault, self.reference, self.transport = vault, credential_reference, transport

    @staticmethod
    def _path(owner, name):
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
            or value in {".", ".."}
            for value in (owner, name)
        ):
            raise ValueError("GitHub 仓库路径无效")
        return "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")

    def _get(self, path, *, params=None, etag=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.reference:
            headers["Authorization"] = "Bearer " + self.vault.read(self.reference).reveal()
        if etag:
            headers["If-None-Match"] = etag
        try:
            with httpx.Client(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=30
            ) as client:
                with client.stream(
                    "GET", self.base_url + path, headers=headers, params=params
                ) as response:
                    if response.status_code == 304:
                        return 304, None, response.headers
                    if not response.is_success:
                        raise GitHubReadError(
                            "authentication_or_permission"
                            if response.status_code in {401, 403}
                            else "not_found"
                            if response.status_code == 404
                            else "http_error"
                        )
                    raw = bytearray()
                    for part in response.iter_bytes():
                        raw.extend(part)
                        if len(raw) > 4 * 1024 * 1024:
                            raise GitHubReadError("response_too_large")
                    return response.status_code, json.loads(raw), response.headers
        except (httpx.HTTPError, ValueError):
            raise GitHubReadError("transport_or_protocol_error") from None

    def repository(self, owner, name):
        _, body, _ = self._get(self._path(owner, name))
        if (
            not isinstance(body, dict)
            or type(body.get("id")) is not int
            or body["id"] <= 0
            or not isinstance(body.get("default_branch"), str)
        ):
            raise GitHubReadError("invalid_repository_identity")
        if not isinstance(body.get("owner"), dict) or not body["default_branch"].strip():
            raise GitHubReadError("invalid_repository_identity")
        actual_owner = body["owner"].get("login")
        actual_name = body.get("name")
        self._path(actual_owner, actual_name)
        return {
            "github_id": str(body["id"]),
            "owner": actual_owner,
            "name": actual_name,
            "default_branch": body["default_branch"],
        }

    def issues_page(self, owner, name, *, page, etag=None):
        if type(page) is not int or not 1 <= page <= 1000000:
            raise ValueError("分页无效")
        status, body, headers = self._get(
            self._path(owner, name) + "/issues",
            params={"state": "all", "per_page": 100, "page": page},
            etag=etag,
        )
        if status == 304:
            return ReadPage(304)
        if (
            not isinstance(body, list)
            or len(body) > 100
            or any(
                not isinstance(item, dict)
                or type(item.get("id")) is not int
                or item["id"] <= 0
                or type(item.get("number")) is not int
                for item in body
            )
        ):
            raise GitHubReadError("invalid_issue_page")
        received = headers.get("etag")
        if received and (len(received) > 512 or "\n" in received or "\r" in received):
            raise GitHubReadError("invalid_etag")
        return ReadPage(status, tuple(body), received, 'rel="next"' in headers.get("link", ""))

    def branch(self, owner, name, branch):
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("目标分支不能为空")
        _, body, _ = self._get(self._path(owner, name) + "/branches/" + quote(branch, safe=""))
        commit = body.get("commit", {}) if isinstance(body, dict) else {}
        digest = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{40}", digest):
            raise GitHubReadError("invalid_branch_sha")
        return digest

    def contribution(self, request: RepositoryRequest):
        _, user, _ = self._get("/user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise GitHubReadError("execution_identity_unconfirmed")
        _, upstream, _ = self._get(self._path(request.owner, request.name))
        if (
            not isinstance(upstream, dict)
            or type(upstream.get("id")) is not int
            or upstream["id"] <= 0
        ):
            raise GitHubReadError("invalid_repository_identity")
        self._path(upstream.get("owner", {}).get("login"), upstream.get("name"))
        if upstream.get("archived") or upstream.get("disabled"):
            raise GitHubReadError("repository_unavailable")
        target = request.target_branch or upstream.get("default_branch")
        sha = self.branch(request.owner, request.name, target)
        maintained = upstream.get("permissions", {}).get("push") is True
        if request.contribution_path == "direct":
            if not maintained:
                raise GitHubReadError("direct_write_permission_missing")
            publication = upstream
        else:
            if upstream.get("private") is not False or upstream.get("allow_forking") is False:
                raise GitHubReadError("public_fork_path_unavailable")
            _, publication, _ = self._get(self._path(request.fork_owner, request.fork_name))
            if not isinstance(publication, dict):
                raise GitHubReadError("invalid_publication_identity")
            self._path(publication.get("owner", {}).get("login"), publication.get("name"))
            parent = publication.get("parent", {})
            if (
                publication.get("fork") is not True
                or parent.get("id") != upstream["id"]
                or publication.get("owner", {}).get("login") != user["login"]
                or publication.get("permissions", {}).get("push") is not True
                or publication.get("archived")
                or type(publication.get("id")) is not int
                or publication["id"] <= 0
                or publication.get("disabled")
            ):
                raise GitHubReadError("fork_write_or_relationship_missing")
        if upstream.get("has_issues") is False:
            raise GitHubReadError("issue_path_disabled")
        self._get(self._path(request.owner, request.name) + "/pulls", params={"per_page": 1})
        return {
            "github_id": str(upstream["id"]),
            "owner": upstream["owner"]["login"],
            "name": upstream["name"],
            "default_branch": upstream.get("default_branch"),
            "target_branch": target,
            "target_sha": sha,
            "private": upstream.get("private"),
            "maintained": maintained,
            "execution_login": user["login"],
            "contribution_path": request.contribution_path,
            "publication": {
                "github_id": str(publication["id"]),
                "owner": publication["owner"]["login"],
                "name": publication["name"],
            },
            "pr_path": "readable_recheck_write_at_delivery",
        }
