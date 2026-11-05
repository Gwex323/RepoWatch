import json
from uuid import uuid4

from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.queue_state import refresh_ready, settle_ready
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, business_event, create_task, required
from repowatch.domain.repositories import AdmissionPolicy, RepositoryRequest, ScanPolicy
from repowatch.domain.task_records import canonical_json, require_time, stable_id


class RepositoryService:
    def __init__(self, store, adapter_factory):
        self.store, self.adapter_factory = store, adapter_factory

    def require_ready(self, token, *, now):
        from repowatch.application.orchestration import Orchestrator

        if self.verify_target(token, now=now):
            return True
        Orchestrator(self.store).wait_for_scan(token, now=now)
        return False

    def verify_target(self, token, *, now):
        from repowatch.application.task_records import execution_record
        from repowatch.domain.scans import readiness

        with self.store.read() as records:
            run = execution_record(records, token, now)
            repository = dict(
                required(
                    records,
                    "repositories",
                    id=required(records, "tasks", id=run["task_id"])["repository_id"],
                )
            )
        adapter = self.adapter_factory(repository["credential_reference"])
        try:
            sha = adapter.branch(
                repository["github_owner"], repository["github_name"], repository["target_branch"]
            )
        except Exception:
            with self.store.write() as records:
                execution_record(records, token, now)
                current = required(records, "repositories", id=repository["id"])
                if current["revision"] == repository["revision"]:
                    settle_ready(records, now)
                    records.change(
                        "repositories",
                        {"id": repository["id"]},
                        {"scan_ready_sha": None, "scan_ready_json": None},
                    )
                block(records, "target_read_failed", run["task_id"], now=now)
            raise PermissionError("目标分支无法读取，旧就绪不可用于执行") from None
        if sha != repository["target_sha"]:
            self.record_target_drift(
                repository["id"],
                sha,
                now=now,
                owner=run["budget_owner_task_id"],
                expected_revision=repository["revision"],
                configuration=json.loads(run["snapshot_json"])["configuration"],
            )
            return False
        with self.store.write() as records:
            execution_record(records, token, now)
            current = required(records, "repositories", id=repository["id"])
            if current["revision"] != repository["revision"]:
                raise ConflictError("目标读取期间配置发生变化，须重读")
            ready = json.loads(repository["scan_ready_json"] or "null") == readiness(repository)
        if ready and repository["identity_json"] is not None:
            from repowatch.application.artifacts import ArtifactService
            from repowatch.application.scans import ScanService

            view = ScanService(
                self.store, None, ArtifactService(self.store, self.store.settings.artifacts_dir)
            ).view(repository["id"])
            ready = any(
                batch["id"] == repository["scan_ready_batch_id"] and batch["current_ready"]
                for batch in view["batches"]
            )
            if not ready:
                with self.store.write() as records:
                    current = required(records, "repositories", id=repository["id"])
                    if current["revision"] == repository["revision"]:
                        settle_ready(records, now)
                        records.change(
                            "repositories",
                            {"id": repository["id"]},
                            {
                                "scan_ready_sha": None,
                                "scan_ready_json": None,
                                "scan_ready_batch_id": None,
                            },
                        )
        return ready

    def record_target_drift(
        self, repository_id, observed_sha, *, now, owner, expected_revision, configuration=None
    ):
        """Invalidate readiness and persist a prerequisite scan for a moved branch."""
        require_time(now)
        with self.store.write() as records:
            repository = dict(required(records, "repositories", id=repository_id))
            if repository["revision"] != expected_revision:
                raise ConflictError("目标读取期间仓库修订变化，须重新读取")
            if observed_sha == repository["target_sha"]:
                return None
            settle_ready(records, now)
            scan_id = create_task(
                records,
                repository_id,
                f"target-drift:{repository['target_revision']}:{repository['import_epoch']}:"
                f"{repository['scan_policy_revision']}:{repository['revision']}:{observed_sha}",
                None,
                "scan",
                now,
                owner=owner,
                input_payload={
                    "target_sha": observed_sha,
                    "target_revision": repository["target_revision"],
                    "import_epoch": repository["import_epoch"],
                    "scan_policy_revision": repository["scan_policy_revision"],
                },
            )
            records.change(
                "tasks",
                {"id": scan_id},
                {"configuration_json": canonical_json(configuration or {})},
            )
            records.change(
                "repositories",
                {"id": repository_id},
                {
                    "target_sha": observed_sha,
                    "scan_ready_sha": None,
                    "scan_ready_json": None,
                    "revision": repository["revision"] + 1,
                },
            )
            old_scans = [
                row["id"]
                for row in records.find("tasks", repository_id=repository_id)
                if row["mode"] == "scan" and row["id"] != scan_id
            ]
            for old_scan in old_scans:
                for dependency in records.find("scan_dependencies", scan_task_id=old_scan):
                    records.remove(
                        "scan_dependencies",
                        waiting_task_id=dependency["waiting_task_id"],
                        scan_task_id=old_scan,
                    )
                    if not records.one(
                        "scan_dependencies",
                        waiting_task_id=dependency["waiting_task_id"],
                        scan_task_id=scan_id,
                    ):
                        records.add(
                            "scan_dependencies",
                            {
                                "waiting_task_id": dependency["waiting_task_id"],
                                "scan_task_id": scan_id,
                            },
                        )
            for task in records.find("tasks", repository_id=repository_id):
                if task["id"] == scan_id or task["mode"] not in {"implement", "amend"}:
                    continue
                if task["state"] in {"ended", "cancelled"}:
                    continue
                if task["wait_reason"] == "target_configuration":
                    continue
                changes = {"revision": task["revision"] + 1, "wait_reason": "scan"}
                if task["state"] == "running":
                    changes["stop_requested"] = 1
                else:
                    changes["state"] = "waiting"
                records.change("tasks", {"id": task["id"]}, changes)
                records.remove("scan_dependencies", waiting_task_id=task["id"])
                if (
                    records.one(
                        "scan_dependencies", waiting_task_id=task["id"], scan_task_id=scan_id
                    )
                    is None
                ):
                    records.add(
                        "scan_dependencies",
                        {"waiting_task_id": task["id"], "scan_task_id": scan_id},
                    )
            business_event(
                records,
                "repository_target_changed",
                scan_id,
                repository_id=repository_id,
                sha=observed_sha,
            )
            refresh_ready(records, now)
            return scan_id

    def preflight(self, operation_id, data, *, now, actor="local-user"):
        human(actor)
        require_time(now)
        request = RepositoryRequest.model_validate(data)
        payload = {"kind": "repository_preflight", "request": request.model_dump()}
        with self.store.read() as records:
            _, replay = replay_operation(records, operation_id, payload)
            if replay is not None:
                return replay
        identity = self.adapter_factory(request.credential_reference).contribution(request)
        result = {"preflight_id": str(uuid4()), "identity": identity, "expires_at": now + 300}
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, payload)
            if replay is not None:
                return replay
            remember_operation(records, operation_id, fingerprint, 1, result)
            records.add(
                "repository_preflights",
                {
                    "id": result["preflight_id"],
                    "operation_id": operation_id,
                    "request_json": canonical_json(request.model_dump()),
                    "identity_json": canonical_json(identity),
                    "created_at": now,
                    "expires_at": now + 300,
                },
            )
        return result

    def save(
        self,
        operation_id,
        preflight_id,
        policy,
        *,
        expected_revision,
        now,
        actor="local-user",
        scan_policy=None,
    ):
        human(actor)
        require_time(now)
        policy = AdmissionPolicy.model_validate(policy).model_dump()
        request = {
            "kind": "repository_save",
            "preflight_id": preflight_id,
            "policy": policy,
            "scan_policy": scan_policy,
            "expected_revision": expected_revision,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            preflight = required(records, "repository_preflights", id=preflight_id)
            if now >= preflight["expires_at"]:
                raise ConflictError("预检已过期，请重新核验路径")
            identity = json.loads(preflight["identity_json"])
            configuration = json.loads(preflight["request_json"])
            old = records.one("repositories", github_id=identity["github_id"])
            current_scan_policy = ScanPolicy.model_validate(
                scan_policy
                if scan_policy is not None
                else json.loads(old["scan_policy_json"])
                if old
                else {}
            ).model_dump()
            if expected_revision != (old["revision"] if old else 0):
                raise ConflictError("仓库修订已变化，请刷新后重新导入")
            if policy["mode"] is None:
                policy["mode"] = "all_bugs" if identity["maintained"] else "labels"
            settle_ready(records, now)
            identifier = old["id"] if old else stable_id("repository", identity["github_id"])
            revision = old["revision"] + 1 if old else 1
            epoch = old["import_epoch"] + 1 if old else 1
            target_changed = bool(
                old
                and (
                    old["target_branch"] != identity["target_branch"]
                    or old["publication_json"] != canonical_json(identity["publication"])
                    or old["contribution_path"] != identity["contribution_path"]
                )
            )
            policy_changed = bool(old and old["policy_json"] != canonical_json(policy))
            scan_policy_changed = bool(
                old
                and ScanPolicy.model_validate(json.loads(old["scan_policy_json"])).model_dump()
                != current_scan_policy
            )
            values = {
                "github_owner": identity["owner"],
                "github_name": identity["name"],
                "default_branch": identity["default_branch"],
                "target_branch": identity["target_branch"],
                "target_sha": identity["target_sha"],
                "import_epoch": epoch,
                "revision": revision,
                "admission_mode": policy["mode"],
                "target_revision": (old["target_revision"] if old else 1) + int(target_changed),
                "admission_revision": (old["admission_revision"] if old else 1)
                + int(policy_changed),
                "scan_policy_revision": (old["scan_policy_revision"] if old else 1)
                + int(scan_policy_changed),
                "scan_policy_json": canonical_json(current_scan_policy),
                "policy_json": canonical_json(policy),
                "identity_json": canonical_json(identity),
                "publication_json": canonical_json(identity["publication"]),
                "contribution_path": identity["contribution_path"],
                "credential_reference": configuration["credential_reference"],
                "scan_ready_sha": None,
                "scan_ready_json": None,
            }
            if old:
                records.change(
                    "repositories", {"id": identifier, "revision": old["revision"]}, values
                )
            else:
                records.add(
                    "repositories", {"id": identifier, "github_id": identity["github_id"], **values}
                )
            snapshot = {**values, "repository_id": identifier, "github_id": identity["github_id"]}
            records.add(
                "repository_revisions",
                {
                    "repository_id": identifier,
                    "revision": revision,
                    "configuration_json": canonical_json(snapshot),
                    "created_at": now,
                },
            )
            task = create_task(
                records, identifier, f"import:{epoch}", None, "scan", now, input_payload=snapshot
            )
            records.add(
                "repository_imports",
                {
                    "id": str(uuid4()),
                    "repository_id": identifier,
                    "import_epoch": epoch,
                    "task_id": task,
                    "configuration_json": canonical_json(snapshot),
                    "created_at": now,
                },
            )
            for existing in records.find("tasks", repository_id=identifier):
                if existing["id"] == task or existing["state"] in {"ended", "cancelled"}:
                    continue
                waiting_on_old_scan = bool(
                    records.find("scan_dependencies", waiting_task_id=existing["id"])
                )
                if existing["mode"] in {"implement", "amend"} or waiting_on_old_scan:
                    changes = (
                        {
                            "stop_requested": 1,
                            "wait_reason": "target_configuration" if target_changed else "scan",
                        }
                        if existing["state"] == "running"
                        else {
                            "state": "waiting",
                            "wait_reason": "target_configuration" if target_changed else "scan",
                        }
                    )
                    records.change(
                        "tasks",
                        {"id": existing["id"]},
                        {**changes, "revision": existing["revision"] + 1},
                    )
                    records.remove("scan_dependencies", waiting_task_id=existing["id"])
                    if not target_changed:
                        records.add(
                            "scan_dependencies",
                            {"waiting_task_id": existing["id"], "scan_task_id": task},
                        )
            result = {
                "repository_id": identifier,
                "revision": revision,
                "import_epoch": epoch,
                "scan_task_id": task,
                "policy": policy,
                "admission_status": "configuration_required"
                if policy["mode"] == "labels" and not policy["allowed_labels"]
                else "configured",
                "target_changed": target_changed,
            }
            remember_operation(
                records, operation_id, fingerprint, max(expected_revision, 1), result
            )
            business_event(
                records,
                "repository_imported",
                task,
                repository_id=identifier,
                import_epoch=epoch,
                target_changed=target_changed,
            )
            refresh_ready(records, now)
            return result
