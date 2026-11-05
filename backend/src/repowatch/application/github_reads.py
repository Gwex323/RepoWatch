"""轻量只读同步的来源记录与待分析信号；不推理、不领取重任务。"""

import json
from datetime import datetime
from uuid import uuid4

from repowatch.adapters.windows_jobs import current_process, process_identity
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.credentials import CredentialError
from repowatch.application.github_ports import GitHubReadError, GitHubReadPort
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords, business_event, required
from repowatch.domain.task_records import canonical_json, content_digest, require_time, stable_id


def issue_input(item):
    return {
        "github_id": str(item["id"]),
        "number": item["number"],
        "title": item.get("title", ""),
        "body": item.get("body") or "",
        "state": item.get("state", "open"),
        "labels": sorted(
            label if isinstance(label, str) else label["name"] for label in item.get("labels", [])
        ),
        "assignees": sorted(person["login"] for person in item.get("assignees", [])),
    }


class GitHubReads:
    def __init__(self, store, port: GitHubReadPort):
        self.store, self.port, self.tasks = store, port, TaskRecords(store)

    def register_identity(self, owner, name, *, admission_mode="manual", actor="local-user"):
        human(actor)
        identity = self.port.repository(owner, name)
        repository = self.tasks.register_repository(
            identity["github_id"], admission_mode=admission_mode
        )
        with self.store.write() as records:
            records.change(
                "repositories",
                {"id": repository},
                {
                    "github_owner": identity["owner"],
                    "github_name": identity["name"],
                    "default_branch": identity["default_branch"],
                },
            )
        return repository

    def status(self, read_id):
        with self.store.read() as records:
            row = required(records, "github_read_operations", id=read_id)
            return {
                "read_id": read_id,
                "state": row["state"],
                "result": json.loads(row["result_json"] or "null"),
                "error": row["error_code"],
            }

    def read_page(self, operation_id, repository_id, *, page=1, now, actor="local-user"):
        human(actor)
        require_time(now)
        request = {
            "kind": "github_read_page",
            "repository_id": repository_id,
            "page": page,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                read_id = replay["read_id"]
                existing = True
            else:
                existing = False
                repository = dict(required(records, "repositories", id=repository_id))
                if not repository["github_owner"] or not repository["github_name"]:
                    raise PermissionError("仓库身份尚未通过只读端口确认")
                read_id = str(uuid4())
                remember_operation(records, operation_id, fingerprint, 1, {"read_id": read_id})
                records.add(
                    "github_read_operations",
                    {
                        "id": read_id,
                        "operation_id": operation_id,
                        "repository_id": repository_id,
                        "page": page,
                        "state": "pending",
                        "owner_process_json": canonical_json(current_process()),
                    },
                )
                cache = records.one("github_read_cache", repository_id=repository_id, page=page)
                cache = dict(cache) if cache else None
                usable_cache = bool(cache and cache["etag"])
                if cache:
                    for identifier in json.loads(cache["observation_ids_json"]):
                        source = records.one("github_observations", id=identifier)
                        if source is None or source["repository_id"] != repository_id:
                            usable_cache = False
        if existing:
            return self.status(read_id)
        try:
            result = self.port.issues_page(
                repository["github_owner"],
                repository["github_name"],
                page=page,
                etag=cache["etag"] if usable_cache else None,
            )
            if result.status == 304 and not usable_cache:
                result = self.port.issues_page(
                    repository["github_owner"], repository["github_name"], page=page
                )
                if result.status == 304:
                    raise GitHubReadError("uncached_304")
            if result.status == 304:
                observations = json.loads(cache["observation_ids_json"])
                has_next = bool(cache["has_next"])
                etag = cache["etag"]
            else:
                observations = [
                    self._observe(repository_id, item, now=now) for item in result.items
                ]
                has_next, etag = result.has_next, result.etag
            response = {
                "observation_ids": observations,
                "has_next": has_next,
                "page_complete": True,
                "repository_snapshot_complete": False,
            }
            with self.store.write() as records:
                values = {
                    "etag": etag,
                    "observation_ids_json": canonical_json(observations),
                    "has_next": int(has_next),
                    "last_success_at": now,
                }
                if cache:
                    records.change(
                        "github_read_cache", {"repository_id": repository_id, "page": page}, values
                    )
                else:
                    records.add(
                        "github_read_cache",
                        {"repository_id": repository_id, "page": page, **values},
                    )
                records.change(
                    "github_read_operations",
                    {"id": read_id},
                    {"state": "completed", "result_json": canonical_json(response)},
                )
                business_event(
                    records,
                    "github_page_read",
                    repository_id=repository_id,
                    page=page,
                    has_next=has_next,
                )
        except (
            GitHubReadError,
            ValueError,
            KeyError,
            ConflictError,
            TypeError,
            AttributeError,
            CredentialError,
        ) as error:
            code = (
                error.code if isinstance(error, GitHubReadError) else "source_conflict_or_invalid"
            )
            with self.store.write() as records:
                records.change(
                    "github_read_operations",
                    {"id": read_id},
                    {"state": "failed", "error_code": code},
                )
        return self.status(read_id)

    def _observe(self, repository_id, item, *, now):
        kind = "pull_request" if "pull_request" in item else "issue"
        object_id = str(item["id"])
        digest = content_digest(item)
        identifier = stable_id("github-observation", repository_id, kind, object_id, digest)
        identity = {"repository_id": repository_id, "kind": kind, "object_id": object_id}
        with self.store.read() as records:
            old = records.one("github_object_heads", **identity)
            previous = (
                required(records, "github_observations", id=old["observation_id"]) if old else None
            )
            if previous and previous["id"] != identifier:
                old_time, new_time = previous["remote_version"], item.get("updated_at")
                if (
                    not old_time
                    or not new_time
                    or datetime.fromisoformat(new_time.replace("Z", "+00:00"))
                    <= datetime.fromisoformat(old_time.replace("Z", "+00:00"))
                ):
                    raise GitHubReadError("source_order_uncertain")
        signal_id = None
        if kind == "issue":
            task = self.tasks.ensure_task(
                repository_id, "github:issue:" + object_id, issue_id=object_id, now=now
            )
            payload = issue_input(item)
            signal_id = self.tasks.observe(
                task, "github:issue:" + object_id, content_digest(payload), payload, now=now
            )
        with self.store.write() as records:
            if records.one("github_observations", id=identifier) is None:
                records.add(
                    "github_observations",
                    {
                        "id": identifier,
                        **identity,
                        "payload_sha256": digest,
                        "payload_json": canonical_json(item),
                        "remote_version": item.get("updated_at"),
                    },
                )
                records.add(
                    "memory_source_facts",
                    {
                        "observation_id": identifier,
                        "repository_id": repository_id,
                        "signal_id": signal_id,
                        "visibility": "repository",
                    },
                )
            if records.one("github_object_heads", **identity):
                records.change("github_object_heads", identity, {"observation_id": identifier})
            else:
                records.add("github_object_heads", {**identity, "observation_id": identifier})
        return identifier

    def reconcile(self):
        with self.store.write() as records:
            for row in records.find("github_read_operations", state="pending"):
                owner = json.loads(row["owner_process_json"])
                if owner.get("platform") == "windows" and owner.get("created") is not None:
                    try:
                        dead = process_identity(owner["pid"]) != owner["created"]
                    except OSError:
                        dead = False
                    if dead:
                        records.change(
                            "github_read_operations",
                            {"id": row["id"]},
                            {"state": "failed", "error_code": "interrupted_read"},
                        )


class MemorySourceFacts:
    def __init__(self, store):
        self.store = store

    def repository_facts(self, repository_id):
        with self.store.read() as records:
            required(records, "repositories", id=repository_id)
            return [
                {
                    "source_id": fact["observation_id"],
                    "repository_id": repository_id,
                    "signal_id": fact["signal_id"],
                    "visibility": "repository",
                    "source": dict(
                        required(records, "github_observations", id=fact["observation_id"])
                    ),
                }
                for fact in records.find("memory_source_facts", repository_id=repository_id)
            ]
