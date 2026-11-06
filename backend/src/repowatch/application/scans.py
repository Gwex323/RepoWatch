import json
from hashlib import sha256

from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import (
    business_event,
    create_task,
    execution_record,
    required,
)
from repowatch.domain.scans import checks_for, inventory, readiness
from repowatch.domain.task_records import canonical_json, content_digest, stable_id


class ScanService:
    def __init__(self, store, objects, artifacts):
        self.store, self.objects, self.artifacts = store, objects, artifacts

    def begin(self, token, sha, environment, *, now, chunk_bytes=16000, exclusions=None):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            task = required(records, "tasks", id=run["task_id"])
            if task["mode"] != "scan":
                raise PermissionError("只有扫描运行可建立扫描批次")
            repository = required(records, "repositories", id=task["repository_id"])
            snapshot = json.loads(run["snapshot_json"])
            version = readiness(repository)
            version.update(
                {
                    key: snapshot["input"].get(key, version[key])
                    for key in ("target_revision", "import_epoch", "scan_policy_revision")
                }
            )
            version["target_sha"] = sha
            repository_id, task_id = task["repository_id"], task["id"]
        manifest = inventory(
            self.objects, repository_id, sha, chunk_bytes=chunk_bytes, exclusions=exclusions
        )
        checks = checks_for(self.objects, repository_id, manifest)
        environment = {
            **environment,
            "manifest_policy": manifest["policy"],
            "configuration_sha256": content_digest(snapshot["configuration"]),
        }
        identifier = stable_id("scan", task_id, version, environment, manifest["policy"])
        with self.store.write() as records:
            execution_record(records, token, now)
            old = records.one("scan_batches", id=identifier)
            if not old:
                records.change(
                    "repositories",
                    {"id": repository_id},
                    {
                        "scan_ready_sha": None,
                        "scan_ready_json": None,
                        "scan_ready_batch_id": None,
                    },
                )
                for previous in records.find("scan_batches", task_id=task_id):
                    records.change("scan_batches", {"id": previous["id"]}, {"state": "superseded"})
                records.add(
                    "scan_batches",
                    {
                        "id": identifier,
                        "task_id": task_id,
                        "repository_id": repository_id,
                        "version_json": canonical_json(version),
                        "manifest_json": canonical_json(manifest),
                        "checks_json": canonical_json(checks),
                        "environment_json": canonical_json(environment),
                        "created_at": now,
                    },
                )
                business_event(records, "scan_batch_created", task_id, batch_id=identifier)
        return identifier

    def batch(self, token, identifier, *, now):
        with self.store.read() as records:
            run = execution_record(records, token, now)
            batch = dict(required(records, "scan_batches", id=identifier))
            if batch["task_id"] != run["task_id"]:
                raise PermissionError("扫描批次不属于当前任务")
        return batch

    def input(self, token, identifier, unit_id, *, now):
        batch = self.batch(token, identifier, now=now)
        units = {unit["id"]: unit for unit in self.units(batch)}
        if unit_id not in units:
            raise PermissionError("扫描块不在固定清单内")
        unit = units[unit_id]
        content = self.objects.blob(batch["repository_id"], unit["oid"])[
            unit["start"] : unit["end"]
        ]
        payload = {"batch_id": identifier, "unit": unit, "content_hex": content.hex()}
        operation_key = "scan-input:" + identifier + ":" + unit_id
        with self.store.read() as records:
            previous_artifacts = records.find("artifact_intents", run_id=token.run_id)
        for previous_artifact in previous_artifacts:
            if previous_artifact["operation_key"].startswith(operation_key):
                if previous_artifact["state"] == "registered":
                    try:
                        self.artifacts.require(
                            previous_artifact["id"], now=now, block_on_failure=False
                        )
                    except (PermissionError, LookupError):
                        pass
        with self.store.read() as records:
            previous = records.find("artifact_intents", run_id=token.run_id)
            invalid = [
                row
                for row in previous
                if row["operation_key"].startswith(operation_key) and row["state"] == "unavailable"
            ]
        if invalid:
            operation_key += ":recollect:" + str(len(invalid))
        artifact = self.artifacts.register(
            token,
            operation_key,
            canonical_json(payload).encode(),
            context={"producer": "scan_input", "batch_id": identifier, "unit_id": unit_id},
            now=now,
        )
        return {
            "artifact_id": artifact,
            "unit": unit,
            "content": content.decode("utf-8", "replace"),
        }

    def units(self, batch):
        with self.store.read() as records:
            splits = {
                row["parent_id"]: json.loads(row["children_json"])
                for row in records.find("scan_splits", batch_id=batch["id"])
            }
        pending, leaves = list(json.loads(batch["manifest_json"])["units"]), []
        while pending:
            unit = pending.pop(0)
            if unit["id"] in splits:
                pending[0:0] = splits[unit["id"]]
            else:
                leaves.append(unit)
        return leaves

    def split(self, token, identifier, unit_id, *, now):
        batch = self.batch(token, identifier, now=now)
        with self.store.read() as records:
            previous = records.one("scan_splits", batch_id=identifier, parent_id=unit_id)
        if previous:
            return json.loads(previous["children_json"])
        unit = next((row for row in self.units(batch) if row["id"] == unit_id), None)
        if unit is None or unit["end"] - unit["start"] <= 256:
            return []
        middle = (unit["start"] + unit["end"]) // 2
        children = []
        content = self.objects.blob(batch["repository_id"], unit["oid"])
        for start, end in ((unit["start"], middle), (middle, unit["end"])):
            child = {**unit, "start": start, "end": end}
            child.pop("id")
            child["sha256"] = sha256(content[start:end]).hexdigest()
            child["id"] = content_digest(child)
            children.append(child)
        with self.store.write() as records:
            execution_record(records, token, now)
            records.add(
                "scan_splits",
                {
                    "batch_id": identifier,
                    "parent_id": unit_id,
                    "children_json": canonical_json(children),
                    "created_at": now,
                },
            )
            business_event(records, "scan_range_split", batch["task_id"], parent_id=unit_id)
        return children

    def accept(self, token, identifier, unit_id, input_artifact_id, payload, *, now):
        batch = self.batch(token, identifier, now=now)
        expected = self.input(token, identifier, unit_id, now=now)
        if expected["artifact_id"] != input_artifact_id:
            raise PermissionError("审查输入不是当前运行实际提供的完整分块")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"input_sha256", "analysis", "findings", "complete"}
            or payload["input_sha256"] != expected["unit"]["sha256"]
            or payload["complete"] is not True
            or not isinstance(payload["analysis"], str)
            or len(payload["analysis"].strip()) < 20
            or not isinstance(payload["findings"], list)
        ):
            return {"accepted": False, "reason": "incomplete_or_invalid_output"}
        for finding in payload["findings"]:
            if (
                not isinstance(finding, dict)
                or set(finding) != {"title", "basis", "start", "end"}
                or not all(
                    isinstance(finding[key], str) and finding[key].strip()
                    for key in ("title", "basis")
                )
                or type(finding["start"]) is not int
                or type(finding["end"]) is not int
                or not expected["unit"]["start"]
                <= finding["start"]
                <= finding["end"]
                <= expected["unit"]["end"]
            ):
                return {"accepted": False, "reason": "candidate_location_or_basis_invalid"}
        with self.store.read() as records:
            previous = records.one("scan_coverage", batch_id=identifier, unit_id=unit_id)
        if previous:
            try:
                self.artifacts.require(
                    previous["input_artifact_id"], now=now, block_on_failure=False
                )
                self.artifacts.require(
                    previous["result_artifact_id"], now=now, block_on_failure=False
                )
                return {"accepted": True, "artifact_id": previous["result_artifact_id"]}
            except (PermissionError, LookupError):
                with self.store.write() as records:
                    execution_record(records, token, now)
                    records.remove("scan_coverage", batch_id=identifier, unit_id=unit_id)
        result_key = "scan-result:" + content_digest(
            {
                "batch": identifier,
                "unit": unit_id,
                "input": input_artifact_id,
                "payload": payload,
            }
        )
        with self.store.read() as records:
            prior_results = [
                row
                for row in records.find("artifact_intents", run_id=token.run_id)
                if row["operation_key"].startswith(result_key)
            ]
        for prior_result in prior_results:
            if prior_result["state"] == "registered":
                try:
                    self.artifacts.require(prior_result["id"], now=now, block_on_failure=False)
                except PermissionError:
                    pass
        with self.store.read() as records:
            invalid_results = [
                row
                for row in records.find("artifact_intents", run_id=token.run_id)
                if row["operation_key"].startswith(result_key) and row["state"] == "unavailable"
            ]
        if invalid_results:
            result_key += ":recollect:" + str(len(invalid_results))
        artifact = self.artifacts.register(
            token,
            result_key,
            canonical_json(payload).encode(),
            context={
                "producer": "scan_review",
                "batch_id": identifier,
                "unit_id": unit_id,
                "input_artifact_id": input_artifact_id,
            },
            now=now,
        )
        with self.store.write() as records:
            execution_record(records, token, now)
            if records.one("scan_coverage", batch_id=identifier, unit_id=unit_id) is None:
                records.add(
                    "scan_coverage",
                    {
                        "batch_id": identifier,
                        "unit_id": unit_id,
                        "input_artifact_id": input_artifact_id,
                        "result_artifact_id": artifact,
                        "run_id": token.run_id,
                    },
                )
                for finding in payload["findings"]:
                    candidate_id = stable_id("candidate", identifier, unit_id, finding)
                    if records.one("scan_candidates", id=candidate_id):
                        continue
                    records.add(
                        "scan_candidates",
                        {
                            "id": candidate_id,
                            "batch_id": identifier,
                            "unit_id": unit_id,
                            "evidence_artifact_id": artifact,
                            "finding_json": canonical_json(
                                {
                                    **finding,
                                    "path": expected["unit"]["path"],
                                    "sha": json.loads(batch["version_json"])["target_sha"],
                                }
                            ),
                        },
                    )
        return {"accepted": True, "artifact_id": artifact}

    def baseline(self, token, identifier, check_id, result, outcome, *, now):
        batch = self.batch(token, identifier, now=now)
        checks = {check["id"]: check for check in json.loads(batch["checks_json"])}
        if check_id not in checks or outcome not in {
            "passed",
            "existing_failed",
            "unable",
            "not_applicable",
        }:
            raise ValueError("检查或结果状态无效")
        if outcome == "not_applicable":
            check = checks[check_id]
            if check.get("applicability") != "not_applicable" or check["argv"]:
                raise PermissionError("声明的检查不能因环境缺失改为不适用")
            operation_key = "scan-inapplicable:" + identifier + ":" + check_id
            with self.store.read() as records:
                previous = records.one("scan_baselines", batch_id=identifier, check_id=check_id)
                if previous:
                    log_id = json.loads(previous["evidence_json"])["log_artifact_id"]
                    intent = records.one("artifact_intents", id=log_id)
                    if intent and intent["state"] == "unavailable":
                        operation_key += ":recollect:" + log_id
            artifact_id = self.artifacts.register(
                token,
                operation_key,
                canonical_json(
                    {"check": check, "version": json.loads(batch["version_json"])}
                ).encode(),
                context={"producer": "host_check_applicability", "batch_id": identifier},
                now=now,
            )
            with self.store.write() as records:
                execution_record(records, token, now)
                records.remove("scan_baselines", batch_id=identifier, check_id=check_id)
                records.add(
                    "scan_baselines",
                    {
                        "batch_id": identifier,
                        "check_id": check_id,
                        "outcome": outcome,
                        "evidence_json": canonical_json(
                            {
                                "log_artifact_id": artifact_id,
                                "basis": check["basis"],
                            }
                        ),
                    },
                )
            return
        artifact = self.artifacts.require(result["log_artifact_id"], run_id=token.run_id, now=now)
        with self.store.read() as records:
            persisted = required(records, "runner_results", command_id=result["command_id"])
            workspace = required(records, "workspaces", id=persisted["workspace_id"])
        environment = json.loads(batch["environment_json"])
        if (
            json.loads(persisted["result_json"]) != result
            or workspace["task_id"] != batch["task_id"]
            or result["sha"] != json.loads(batch["version_json"])["target_sha"]
            or result["image"] != environment["image"]
            or result["argv"] != checks[check_id]["argv"]
        ):
            raise PermissionError("基线证据的命令、环境或版本不匹配")
        from repowatch.domain.scans import baseline_outcome

        actual = baseline_outcome(
            result, (self.artifacts.root / artifact["relative_path"]).read_bytes()
        )
        if actual != outcome:
            raise PermissionError("基线状态与实际 Runner 结果不同")
        with self.store.write() as records:
            execution_record(records, token, now)
            records.remove("scan_baselines", batch_id=identifier, check_id=check_id)
            records.add(
                "scan_baselines",
                {
                    "batch_id": identifier,
                    "check_id": check_id,
                    "outcome": outcome,
                    "evidence_json": canonical_json(result),
                },
            )

    def gaps(self, token, identifier, *, now):
        batch = self.batch(token, identifier, now=now)
        manifest = json.loads(batch["manifest_json"])
        with self.store.read() as records:
            coverage = [dict(row) for row in records.find("scan_coverage", batch_id=identifier)]
            baselines = [dict(row) for row in records.find("scan_baselines", batch_id=identifier)]
        valid_units, valid_checks = set(), set()
        for row in coverage:
            try:
                for key in ("input_artifact_id", "result_artifact_id"):
                    self.artifacts.require(row[key], now=now, block_on_failure=False)
                valid_units.add(row["unit_id"])
            except PermissionError:
                pass
        for row in baselines:
            if row["outcome"] in {"passed", "existing_failed", "not_applicable"}:
                try:
                    self.artifacts.require(
                        json.loads(row["evidence_json"])["log_artifact_id"],
                        now=now,
                        block_on_failure=False,
                    )
                    valid_checks.add(row["check_id"])
                except PermissionError:
                    pass
        return {
            "units": [unit for unit in self.units(batch) if unit["id"] not in valid_units],
            "checks": [
                check
                for check in json.loads(batch["checks_json"])
                if check["id"] not in valid_checks
            ],
            "materialization": [
                entry for entry in manifest["entries"] if entry["disposition"] == "gap"
            ],
            "covered": len(valid_units),
            "total": len(self.units(batch)),
            "missing_materialization": len(
                [entry for entry in manifest["entries"] if entry["disposition"] == "gap"]
            ),
        }

    def record_attempt(
        self, token, identifier, unit_id, input_artifact_id, outcome, *, now, attempt=0
    ):
        if outcome not in {"covered", "no_progress", "invalid_output", "environment"}:
            raise ValueError("扫描尝试结果无效")
        self.batch(token, identifier, now=now)
        with self.store.write() as records:
            execution_record(records, token, now)
            identifier_key = stable_id(
                "scan-attempt",
                identifier,
                token.run_id,
                unit_id,
                input_artifact_id,
                outcome,
                attempt,
            )
            if records.one("scan_attempts", id=identifier_key):
                return identifier_key
            records.add(
                "scan_attempts",
                {
                    "id": identifier_key,
                    "batch_id": identifier,
                    "run_id": token.run_id,
                    "unit_id": unit_id,
                    "input_artifact_id": input_artifact_id,
                    "outcome": outcome,
                    "created_at": now,
                },
            )
            return identifier_key

    def attempts(self, token, identifier, *, now):
        self.batch(token, identifier, now=now)
        with self.store.read() as records:
            return [dict(row) for row in records.find("scan_attempts", batch_id=identifier)]

    def validate_complete(self, token, identifier, *, now):
        gaps = self.gaps(token, identifier, now=now)
        if gaps["units"] or gaps["checks"] or gaps["materialization"]:
            raise PermissionError("扫描仍有输入、结果、检查或物化缺口")
        batch = self.batch(token, identifier, now=now)
        if batch["state"] == "superseded":
            raise ConflictError("扫描批次已被新的环境或策略批次替代")
        with self.store.read() as records:
            run = execution_record(records, token, now)
            task = required(records, "tasks", id=run["task_id"])
            expected = json.loads(batch["environment_json"])["configuration_sha256"]
            if expected != content_digest(json.loads(task["configuration_json"])):
                raise ConflictError("环境或工具配置变化，须重新核验扫描批次")
        return batch

    def decide_candidate(
        self, operation_id, identifier, action, *, expected_revision, now, actor="local-user"
    ):
        human(actor)
        if action not in {"select", "exclude"}:
            raise ValueError("候选操作无效")
        request = {
            "kind": "candidate",
            "id": identifier,
            "action": action,
            "revision": expected_revision,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            candidate = required(records, "scan_candidates", id=identifier)
            if candidate["revision"] != expected_revision:
                raise ConflictError("候选修订变化，请刷新")
            batch = required(records, "scan_batches", id=candidate["batch_id"])
            task_id = candidate["request_task_id"]
            if action == "select" and not task_id:
                task_id = create_task(
                    records,
                    batch["repository_id"],
                    "candidate:" + identifier,
                    None,
                    "triage",
                    now,
                    input_payload={
                        "candidate_id": identifier,
                        "issue_lookup_required": True,
                        "finding": json.loads(candidate["finding_json"]),
                    },
                )
                records.change("tasks", {"id": task_id}, {"selected": 1})
                repository = required(records, "repositories", id=batch["repository_id"])
                if json.loads(repository["scan_ready_json"] or "null") != readiness(repository):
                    current_scans = [
                        row
                        for row in records.find(
                            "tasks", repository_id=repository["id"], mode="scan"
                        )
                        if row["state"] not in {"ended", "cancelled"}
                        and all(
                            json.loads(row["input_json"]).get(key) == repository[key]
                            for key in (
                                "target_sha",
                                "target_revision",
                                "import_epoch",
                                "scan_policy_revision",
                            )
                        )
                    ]
                    scan_id = current_scans[0]["id"] if current_scans else batch["task_id"]
                    records.change(
                        "tasks",
                        {"id": task_id},
                        {"state": "waiting", "wait_reason": "scan"},
                    )
                    records.add(
                        "scan_dependencies",
                        {"waiting_task_id": task_id, "scan_task_id": scan_id},
                    )
            records.change(
                "scan_candidates",
                {"id": identifier},
                {
                    "state": "selected" if action == "select" else "excluded",
                    "revision": candidate["revision"] + 1,
                    "request_task_id": task_id,
                },
            )
            if action == "exclude" and task_id:
                task = required(records, "tasks", id=task_id)
                records.change(
                    "tasks",
                    {"id": task_id},
                    {
                        "user_excluded": 1,
                        "stop_requested": int(task["state"] == "running"),
                        "revision": task["revision"] + 1,
                    },
                )
            result = {"candidate_id": identifier, "request_task_id": task_id, "action": action}
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(records, "scan_candidate_decided", task_id, **result)
            return result

    def view(self, repository_id):
        from repowatch.application.console import public_record

        with self.store.read() as records:
            repository = dict(required(records, "repositories", id=repository_id))
            batches = [
                public_record(row)
                for row in records.find("scan_batches", repository_id=repository_id)
            ]
            for batch in batches:
                source_task = required(records, "tasks", id=batch["task_id"])
                batch["environment_current"] = batch["environment"].get(
                    "configuration_sha256"
                ) == content_digest(json.loads(source_task["configuration_json"]))
                batch["coverage"] = [
                    public_record(row)
                    for row in records.find("scan_coverage", batch_id=batch["id"])
                ]
                batch["baselines"] = [
                    public_record(row)
                    for row in records.find("scan_baselines", batch_id=batch["id"])
                ]
                batch["attempts"] = [
                    public_record(row)
                    for row in records.find("scan_attempts", batch_id=batch["id"])
                ]
                batch["candidates"] = [
                    public_record(row)
                    for row in records.find("scan_candidates", batch_id=batch["id"])
                ]
        from repowatch.adapters.storage.artifact_files import verify_file

        with self.store.read() as records:
            artifacts = {row["id"]: dict(row) for row in records.find("artifacts")}

        def valid(identifier):
            artifact = artifacts.get(identifier)
            return bool(
                artifact
                and artifact["available"]
                and verify_file(self.artifacts.root, artifact["relative_path"], artifact["sha256"])[
                    "valid"
                ]
            )

        for batch in batches:
            original = {"id": batch["id"], "manifest_json": canonical_json(batch["manifest"])}
            batch["manifest"]["units"] = self.units(original)
            batch["recorded_coverage"] = batch["coverage"]
            batch["coverage"] = [
                row
                for row in batch["coverage"]
                if valid(row["input_artifact_id"]) and valid(row["result_artifact_id"])
            ]
            batch["evidence_valid"] = (
                len(batch["coverage"]) == len(batch["manifest"]["units"])
                and len(batch["baselines"]) == len(batch["checks"])
                and all(
                    row["outcome"] in {"passed", "existing_failed", "not_applicable"}
                    and valid(row["evidence"]["log_artifact_id"])
                    for row in batch["baselines"]
                )
                and not any(entry["disposition"] == "gap" for entry in batch["manifest"]["entries"])
            )
            batch["current_version"] = batch["version"] == readiness(repository)
            batch["current_ready"] = (
                batch["state"] == "completed"
                and batch["evidence_valid"]
                and batch["current_version"]
                and json.loads(repository["scan_ready_json"] or "null") == readiness(repository)
                and repository["scan_ready_batch_id"] == batch["id"]
                and batch["environment_current"]
            )
        return {"batches": batches}
