"""正式修复协调器；处理、Runner 与独审在同一授权内串行交接。"""

import json
from uuid import uuid4

from langchain_core.tools import tool

from repowatch.adapters.docker_workspaces import DockerWorkspaces, source_path
from repowatch.adapters.git_objects import GitObjects
from repowatch.application.acceptance import acceptance_view
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.delegation import Delegation
from repowatch.application.github_reads import issue_input
from repowatch.application.issue_context import IssueContexts
from repowatch.application.progress import ProgressService
from repowatch.application.repair_evidence import RepairEvidence, file_manifest
from repowatch.application.task_records import execution_record, required
from repowatch.application.workspaces import WorkspaceService
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.dependencies import dependency_plan
from repowatch.domain.scans import checks_for, inventory
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.domain.triage import TriageAnalysis, classify_analysis, host_triage_gate
from repowatch.domain.verification import (
    ProbePlan,
    ReviewDecision,
    compare_regression,
    probe_verdict,
    weakening_paths,
)
from repowatch.runtime.engine import StagePlan, ToolBinding
from repowatch.worker.runner import Runner


class RepairCoordinator:
    def __init__(self, services, runtime):
        self.services, self.runtime = services, runtime
        self.objects = GitObjects(services.settings.data_dir / "git-objects")
        self.backend = DockerWorkspaces(services.settings.data_dir / "workspaces", self.objects)
        self.workspaces = WorkspaceService(services.store, self.backend)
        self.runner = Runner(services, self.workspaces, clock=services.clock)
        self.evidence = RepairEvidence(services, self.workspaces)
        self.executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)

    def context(self, token):
        with self.services.store.read() as records:
            run = execution_record(records, token, self.services.clock())
            task = dict(required(records, "tasks", id=run["task_id"]))
            repository = dict(required(records, "repositories", id=task["repository_id"]))
            snapshot = json.loads(run["snapshot_json"])
            acceptance = acceptance_view(records, task)
        return task, repository, snapshot, acceptance

    def run(self, token):
        services = self.services
        task, repository, snapshot, acceptance = self.context(token)
        with services.store.read() as records:
            pending = records.one("publication_intents", task_id=task["id"], kind="pull_request")
        if pending and (
            pending["state"] in {"dispatched", "unknown"}
            or snapshot["mode"] == "reconcile_publication"
        ):
            from repowatch.application.publication import Publisher

            return Publisher(services, self.evidence).reconcile_pending(token, pending)
        if task["issue_id"] is None or not acceptance or not acceptance["current"]:
            raise PermissionError("修复须先确认 Issue 与当前验收规范")
        if acceptance["kind"] != "bug" and not acceptance["approved"]:
            raise PermissionError("功能及重构未获当前方案批准")
        if not services.repositories.require_ready(token, now=services.clock()):
            return {"state": "waiting", "reason": "scan"}
        remote = IssueContexts(services).read(token)
        gate = host_triage_gate(repository, task, issue_input(remote["facts"]["issue"]))
        if gate:
            return self.wait(token, gate.reason_code)
        saved_triage = self.latest_triage(task)
        analysis = TriageAnalysis.model_validate(saved_triage["analysis"])
        decision = classify_analysis(analysis, acceptance=acceptance, remote_context=remote)
        if decision.result != "auto_repair":
            return self.wait(token, decision.reason_code)
        external_merge = decision.reason_code == "external_merge_verification"
        merged_pull = None
        base = repository["target_sha"]
        fetched = self.executor.execute(
            token,
            "repair-fetch:" + token.run_id,
            BudgetAmounts(active_ms=180000),
            lambda: self.objects.fetch(
                repository["id"],
                repository["github_owner"],
                repository["github_name"],
                repository["target_branch"],
                secret=services.vault.read(repository["credential_reference"]),
            ),
            lambda _: {},
            purpose="tool",
        )
        if fetched != base:
            services.repositories.record_target_drift(
                repository["id"],
                fetched,
                now=services.clock(),
                owner=task["budget_owner_task_id"],
                expected_revision=repository["revision"],
                configuration=snapshot["configuration"],
            )
            return self.wait(token, "scan")
        if external_merge:
            merged_pull = self.merged_pull(repository, remote, decision.evidence["pr_id"])
            if merged_pull is None:
                return self.wait(token, "external_merge_context_changed")
        base_files = {
            entry["path"]: (entry["mode"], self.objects.blob(repository["id"], entry["oid"]))
            for entry in self.objects.entries(repository["id"], base)
            if entry["mode"] != "160000"
        }
        if len(base_files) != len(self.objects.entries(repository["id"], base)):
            raise PermissionError("子模块内容不可交付，不能开始修复")
        image = snapshot["configuration"].get("workspace_image", "alpine/git:latest")
        workspace = self.create(token, base, image)
        if not workspace["ready"]:
            raise PermissionError("工作区物化失败")
        manifest = inventory(self.objects, repository["id"], base)
        self.preparation = dependency_plan(self.objects, repository["id"], manifest)
        checks = checks_for(self.objects, repository["id"], manifest)
        parent_sha = base
        if pending and pending["state"] == "confirmed":
            previous_request = json.loads(pending["request_json"])
            remote_prs = [
                pull
                for pull in remote["facts"]["pull_requests"]
                if str(pull["id"]) == remote["managed_pr_id"]
            ]
            if len(remote_prs) != 1 or remote_prs[0]["head"]["sha"] != previous_request["head_sha"]:
                raise PermissionError("原 PR head 已变化，须先核对并整合外部内容")
            parent_sha = previous_request["head_sha"]
            if (
                self.objects.run(
                    self.objects.repository(repository["id"]), "merge-base", base, parent_sha
                )
                .decode()
                .strip()
                != base
            ):
                raise PermissionError("当前 base 不在原 PR head 历史中，必须先整合重验")
        self.claim(token, task, repository, remote)
        if json.loads(repository["policy_json"]).get("claim_comment"):
            remote = IssueContexts(services).read(token)
        baseline = []
        for check in checks:
            if check["argv"]:
                baseline.append(
                    self.check(token, workspace, check["argv"], "baseline:" + check["id"])
                )
        probe = self.plan_probe(token, workspace, acceptance)
        probe_files = dict(base_files)
        for path, content in probe.files.items():
            source_path(path)
            if path in base_files and not self.probe_path(path):
                raise PermissionError("探针不能改写实现源码")
            if path not in base_files and not self.probe_path(path):
                raise PermissionError("新增探针须位于测试或受控探针路径")
            probe_files[path] = ("100644", content.encode())
        probe_review = self.review(
            token,
            workspace,
            {
                "phase": "probe",
                "acceptance": acceptance,
                "probe": probe.model_dump(),
                "base_files": {
                    path: base_files[path][1].decode("utf-8", "replace")
                    for path in probe.files
                    if path in base_files
                },
            },
            phase="probe",
        )
        if (
            not probe_review["decision"].passed
            or not probe_review["decision"].probe_valid
            or probe_review["decision"].weakened_acceptance
        ):
            raise PermissionError("独审拒绝探针材料或发现夹带实现修改")
        probe_commit = self.objects.commit_files(
            repository["id"], base, probe_files, message="RepoWatch verification probe"
        )
        probe_workspace = self.create(token, probe_commit["sha"], workspace["image"], mode="check")
        before = self.check(token, probe_workspace, probe.argv, "probe:base", timeout=probe.timeout)
        before_outcome = probe_verdict(before[0], before[1], probe, side="base")
        merged_probe_outcome = (
            probe_verdict(before[0], before[1], probe, side="candidate") if external_merge else None
        )
        if external_merge and merged_probe_outcome == "passed":
            if not services.repositories.require_ready(token, now=services.clock()):
                return {"state": "waiting", "reason": "scan"}
            latest_remote = IssueContexts(services).read(token)
            if latest_remote["digest"] != remote["digest"]:
                return self.wait(token, "external_merge_context_changed")
            if acceptance["kind"] == "refactor":
                if not probe.behavior_argv:
                    raise PermissionError("纯重构缺少行为保持命令")
                behavior = self.check(
                    token, probe_workspace, probe.behavior_argv, "behavior:external-merge"
                )
                if not compare_regression(behavior[0], behavior[0], behavior[1], behavior[1])[
                    "passed"
                ]:
                    raise PermissionError("外部重构的行为保持未核实")
            self.record_progress(
                token,
                "external-merge-resolved:" + str(merged_pull["id"]),
                {
                    "pr_id": str(merged_pull["id"]),
                    "merge_commit_sha": merged_pull["merge_commit_sha"],
                    "base_sha": base,
                    "probe": probe.model_dump(),
                    "result": before[0],
                    "probe_review_artifact_id": probe_review["artifact_id"],
                    "context_digest": remote["digest"],
                },
            )
            services.queue.complete(
                token, now=services.clock(), state="ended", outcome="externally_resolved"
            )
            return {"state": "ended", "reason": "externally_resolved"}
        if before_outcome != "target_failed":
            if external_merge:
                return self.wait(token, "external_merge_verification_inconclusive")
            else:
                raise PermissionError("未取得有效修改前证明：" + before_outcome)
        reproduced = self.record_progress(
            token,
            "reproduced:" + content_digest(probe.model_dump()),
            {
                "base_sha": base,
                "probe": probe.model_dump(),
                "result": before[0],
            },
        )
        if acceptance["kind"] == "refactor":
            if not probe.behavior_argv:
                raise PermissionError("纯重构缺少行为保持命令")
            behavior_before = self.check(
                token, probe_workspace, probe.behavior_argv, "behavior:base"
            )
            if behavior_before[0]["exit_code"] != 0:
                raise PermissionError("纯重构修改前行为检查未通过")
        else:
            behavior_before = None
        findings = []
        frozen = None
        round_index = 0
        if parent_sha != base:
            workspace = self.create(token, parent_sha, workspace["image"])
        while True:
            round_index += 1
            self.executor.execute(
                token,
                "repair-round:" + uuid4().hex,
                BudgetAmounts(repair_rounds=1),
                lambda: None,
                lambda _: {"repair_rounds": 1},
                purpose="counter",
            )
            if round_index > 1:
                if frozen is None:
                    raise PermissionError("返修缺少前一候选")
                workspace = self.create(token, frozen["sha"], workspace["image"])
            for path, content in probe.files.items():
                self.backend.write_source(workspace, task["id"], path, content.encode())
            self.implement(token, workspace, acceptance, probe, findings, round_index)
            session_id, frozen = self.evidence.freeze(
                token,
                workspace,
                parent_sha=parent_sha,
                base_sha=base,
                message="RepoWatch: fix issue #" + str(snapshot["input"]["number"]),
            )
            candidate = self.create(token, frozen["sha"], workspace["image"], mode="check")
            after = self.check(
                token,
                candidate,
                probe.argv,
                "probe:candidate:" + str(round_index),
                timeout=probe.timeout,
            )
            after_outcome = probe_verdict(after[0], after[1], probe, side="candidate")
            comparisons, regression = [], []
            for index, check in enumerate(item for item in checks if item["argv"]):
                current = self.check(
                    token,
                    candidate,
                    check["argv"],
                    "regression:" + str(round_index) + ":" + check["id"],
                )
                regression.append(current)
                comparisons.append(
                    compare_regression(
                        baseline[index][0], current[0], baseline[index][1], current[1]
                    )
                )
            actual = self.backend.snapshot(workspace, task["id"])
            changes = [
                (path, base_files.get(path, ("", b""))[1], actual.get(path, ("", b""))[1])
                for path in sorted(set(base_files) | set(actual))
                if base_files.get(path) != actual.get(path)
            ]
            weakened = weakening_paths(changes)
            behavior_passed = True
            behavior_after = None
            if behavior_before:
                behavior_after = self.check(
                    token, candidate, probe.behavior_argv, "behavior:candidate:" + str(round_index)
                )
                behavior_passed = compare_regression(
                    behavior_before[0], behavior_after[0], behavior_before[1], behavior_after[1]
                )["passed"]
            review_input = {
                "phase": "candidate",
                "acceptance": acceptance,
                "probe": probe.model_dump(),
                "base_probe": before,
                "candidate_probe": after,
                "regressions": comparisons,
                "regression_results": {"base": baseline, "candidate": regression},
                "weakening_paths": weakened,
                "diff": [
                    {
                        "path": path,
                        "before": old.decode("utf-8", "replace"),
                        "after": new.decode("utf-8", "replace"),
                    }
                    for path, old, new in changes
                ],
                "frozen": frozen,
                "behavior_base": behavior_before,
                "behavior_candidate": behavior_after,
            }
            reviewed = self.review(
                token, candidate, review_input, phase="candidate:" + str(round_index)
            )
            decision = reviewed["decision"]
            if decision.unrelated_failures:
                comparisons = [
                    compare_regression(
                        old[0],
                        new[0],
                        old[1],
                        new[1],
                        unrelated_failures=decision.unrelated_failures,
                    )
                    for old, new in zip(baseline, regression, strict=True)
                ]
            passed = (
                after_outcome == "passed"
                and all(item["passed"] for item in comparisons)
                and behavior_passed
                and decision.passed
                and decision.probe_valid
                and not decision.weakened_acceptance
                and not weakened
            )
            artifacts = [
                before[0]["log_artifact_id"],
                after[0]["log_artifact_id"],
                reviewed["artifact_id"],
                probe_review["artifact_id"],
                *[item[0]["log_artifact_id"] for item in baseline + regression],
            ]
            if behavior_before and behavior_after:
                artifacts.extend(
                    [behavior_before[0]["log_artifact_id"], behavior_after[0]["log_artifact_id"]]
                )
            if passed:
                validated = self.record_progress(
                    token,
                    "validated-candidate:" + frozen["manifest_sha256"],
                    {
                        "base_probe": before[0],
                        "candidate_probe": after[0],
                        "comparisons": comparisons,
                    },
                )
                self.record_attempt(
                    token, frozen, after_outcome, comparisons, [reproduced, validated]
                )
                evidence = {
                    "passed": True,
                    "review_passed": True,
                    "review_run_id": reviewed["run_id"],
                    "artifact_ids": artifacts,
                    "probe": probe.model_dump(),
                    "base_probe": before[0],
                    "candidate_probe": after[0],
                    "behavior_base": behavior_before[0] if behavior_before else None,
                    "behavior_candidate": behavior_after[0] if behavior_after else None,
                    "probe_review_artifact_id": probe_review["artifact_id"],
                    "external_merge": {
                        "pr_id": str(merged_pull["id"]),
                        "context_digest": remote["digest"],
                        "base_sha": base,
                        "target_still_fails": True,
                    }
                    if external_merge
                    else None,
                    "comparisons": comparisons,
                    "review": decision.model_dump(),
                }
                self.evidence.confirm(token, session_id, evidence)
                from repowatch.application.publication import Publisher

                return Publisher(services, self.evidence).publish(token, session_id, remote)
            resolved = []
            for old, new in zip(baseline, regression, strict=True):
                comparison = compare_regression(old[0], new[0], old[1], new[1])
                if comparison["passed"] and old[0]["exit_code"] != 0 and new[0]["exit_code"] == 0:
                    resolved.append(
                        self.record_progress(
                            token,
                            "resolved-check:"
                            + content_digest({"argv": new[0]["argv"], "old": old[0]["log_sha256"]}),
                            {"base": old[0], "candidate": new[0]},
                        )
                    )
            if after_outcome == "passed":
                resolved.append(
                    self.record_progress(
                        token,
                        "target-fixed:" + content_digest(probe.model_dump()),
                        {"before": before[0], "after": after[0]},
                    )
                )
            self.record_attempt(token, frozen, after_outcome, comparisons, [reproduced, *resolved])
            findings = [
                *decision.findings,
                after_outcome,
                *[item["reason"] for item in comparisons if not item["passed"]],
                *weakened,
            ]
            with services.store.write() as records:
                records.change("repair_sessions", {"id": session_id}, {"state": "blocked"})
            self.workspaces.remove(token, candidate["id"], now=services.clock())

    def record_attempt(self, token, frozen, outcome, comparisons, progress_ids):
        payload = {
            "producer": "host_stage_verifier",
            "objective_key": "repair",
            "status": "completed",
            "comparison": {
                "manifest_sha256": frozen["manifest_sha256"],
                "probe_outcome": outcome,
                "regressions": comparisons,
            },
            "verified_fact_ids": progress_ids,
        }
        artifact_id = self.services.artifacts.register(
            token,
            "repair-attempt:" + uuid4().hex,
            canonical_json(payload).encode(),
            context={"producer": "host_stage_verifier", "objective_key": "repair"},
            now=self.services.clock(),
        )
        result = ProgressService(
            self.services.store, self.services.artifacts, self.services.budgets
        ).record(token, "repair", artifact_id, now=self.services.clock())
        if result.get("blocked"):
            raise PermissionError("修复触发持久无进展或振荡阻塞")

    def record_progress(self, token, fact_key, payload):
        with self.services.store.read() as records:
            run = execution_record(records, token, self.services.clock())
            snapshot_hash = content_digest(json.loads(run["snapshot_json"]))
        artifact_id = self.services.artifacts.register(
            token,
            "repair-fact:" + uuid4().hex,
            canonical_json(payload).encode(),
            context={
                "producer": "host_verifier",
                "objective_key": "repair",
                "kind": "validated_evidence",
                "fact_key": fact_key,
                "run_snapshot_sha256": snapshot_hash,
            },
            now=self.services.clock(),
        )
        return self.services.budgets.record_verified_progress(
            token,
            objective_key="repair",
            fact_key=fact_key,
            kind="validated_evidence",
            artifact_id=artifact_id,
            now=self.services.clock(),
        )

    def claim(self, token, task, repository, remote):
        from repowatch.application.publication import GitHubPublisher

        policy = json.loads(repository["policy_json"])
        if not policy.get("claim_comment"):
            return
        services = self.services
        port = GitHubPublisher(
            vault=services.vault, credential_reference=repository["credential_reference"]
        )
        with services.store.read() as records:
            intent = records.one("publication_intents", task_id=task["id"], kind="claim")
        marker = "<!-- repowatch-claim:" + task["id"] + " -->"
        identity = json.loads(repository["identity_json"])
        existing = [
            item
            for item in remote["facts"].get("comments", [])
            if marker in (item.get("body") or "")
            and item.get("user", {}).get("login") == identity["execution_login"]
        ]
        if existing:
            if len(existing) != 1:
                raise PermissionError("认领评论不唯一，须先核对")
            if intent:
                with services.store.write() as records:
                    execution_record(records, token, services.clock())
                    records.change(
                        "publication_intents",
                        {"id": intent["id"]},
                        {
                            "state": "confirmed",
                            "result_json": canonical_json({"id": existing[0]["id"]}),
                        },
                    )
            return
        if intent and intent["state"] != "intent":
            raise PermissionError("认领评论回执未知，须先核对而非重复创建")
        identifier = intent["id"] if intent else str(uuid4())
        body = policy["claim_template"] + "\n\n" + marker
        if not intent:
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.add(
                    "publication_intents",
                    {
                        "id": identifier,
                        "task_id": task["id"],
                        "repository_id": repository["id"],
                        "issue_id": task["issue_id"],
                        "kind": "claim",
                        "request_json": canonical_json({"body": body}),
                        "request_sha256": content_digest({"body": body}),
                        "state": "intent",
                        "created_at": services.clock(),
                    },
                )

        def send():
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.change("publication_intents", {"id": identifier}, {"state": "dispatched"})
            try:
                receipt = port.write(
                    "POST",
                    port._path(repository["github_owner"], repository["github_name"])
                    + "/issues/"
                    + str(json.loads(task["input_json"])["number"])
                    + "/comments",
                    {"body": body},
                )
            except Exception:
                with services.store.write() as records:
                    records.change("publication_intents", {"id": identifier}, {"state": "unknown"})
                raise
            with services.store.write() as records:
                records.change(
                    "publication_intents",
                    {"id": identifier},
                    {"state": "confirmed", "result_json": canonical_json({"id": receipt["id"]})},
                )
            return receipt

        self.executor.execute(
            token,
            "claim:" + identifier,
            BudgetAmounts(active_ms=60000),
            send,
            lambda _: {},
            purpose="tool",
        )

    def latest_triage(self, task):
        with self.services.store.read() as records:
            decisions = records.find("triage_decisions", task_id=task["id"])
        matching = [
            item
            for item in decisions
            if item["input_revision"] == task["input_revision"]
            and item["feedback_revision"] == task["feedback_revision"]
        ]
        if not matching:
            raise PermissionError("缺少当前输入的分诊证据")
        return json.loads(matching[-1]["evidence_json"])

    def merged_pull(self, repository, remote, pull_id):
        pulls = [
            item for item in remote["facts"]["pull_requests"] if str(item.get("id")) == str(pull_id)
        ]
        if len(pulls) != 1:
            return None
        pull = pulls[0]
        merge_sha = pull.get("merge_commit_sha")
        if (
            not (pull.get("merged_at") or pull.get("merged"))
            or pull.get("base", {}).get("ref") != repository["target_branch"]
            or not isinstance(merge_sha, str)
            or len(merge_sha) != 40
            or any(character not in "0123456789abcdef" for character in merge_sha)
        ):
            return None
        bare = self.objects.repository(repository["id"])
        try:
            resolved = self.objects.run(bare, "rev-parse", "--verify", merge_sha + "^{commit}")
            if resolved.decode().strip() != merge_sha:
                return None
            ancestor = self.objects.run(
                bare,
                "merge-base",
                "--is-ancestor",
                merge_sha,
                repository["target_sha"],
            )
        except Exception:
            return None
        if ancestor != b"":
            return None
        return pull

    @staticmethod
    def probe_path(path):
        return path.startswith(("tests/", "test/", ".repowatch-probes/")) or path.rsplit("/", 1)[
            -1
        ].startswith("test_")

    def create(self, token, sha, image, *, mode=None):
        return self.executor.execute(
            token,
            "workspace:" + uuid4().hex,
            BudgetAmounts(active_ms=180000),
            lambda: self.workspaces.create(
                token, sha, image=image, mode=mode, now=self.services.clock()
            ),
            lambda _: {},
            purpose="tool",
        )

    def check(self, token, workspace, argv, key, *, timeout=60):
        result = self.runner.check(
            token,
            workspace["id"],
            argv,
            timeout=timeout,
            operation_key=token.run_id + ":" + key,
            preparation=self.preparation,
        )
        artifact = self.services.artifacts.require(
            result["log_artifact_id"], now=self.services.clock()
        )
        return result, (self.services.artifacts.root / artifact["relative_path"]).read_text(
            encoding="utf-8", errors="replace"
        )

    def code(self, workspace):
        return {
            "base_sha": workspace["sha"],
            "tree_sha": self.objects.run(
                self.objects.repository(workspace["repository_id"]),
                "rev-parse",
                workspace["sha"] + "^{tree}",
            )
            .decode()
            .strip(),
            "content_manifest_sha256": workspace["manifest_sha256"],
        }

    def plan_probe(self, token, workspace, acceptance):
        outputs = []

        @tool
        def read_source(path: str) -> str:
            """读取固定 base 源码；path 为 . 时返回文件列表。"""
            if path == ".":
                return canonical_json([item["path"] for item in workspace["manifest"]])
            return self.backend.read_source(workspace, workspace["task_id"], path).decode(
                "utf-8", "replace"
            )

        @tool
        def write_analysis(plan: ProbePlan) -> str:
            """保存独立回归探针及两侧明确成功／目标失败标记。"""
            outputs.append(plan)
            return "探针计划已保存，仍待独审与真实执行"

        def verify(result, events):
            if not outputs or not any(event["tool"] == "read_source" for event in events):
                raise PermissionError("缺少实际源码读取或探针计划")
            return {"comparison": {"probe": outputs[-1].model_dump()}, "verified_fact_ids": []}

        self.runtime.run(
            token,
            StagePlan(
                "probe-plan",
                "repair",
                "读取源码并提出可执行探针，不修改实现；Bug/功能证明目标前失败后通过，"
                "重构使用结构目标并另提供 behavior_argv。验收：" + canonical_json(acceptance),
                "reproduce",
                "processor",
                (
                    ToolBinding(read_source, BudgetAmounts(active_ms=1000)),
                    ToolBinding(write_analysis, BudgetAmounts(active_ms=1000)),
                ),
                verify,
                code_version=self.code(workspace),
                record_attempt=False,
            ),
        )
        return outputs[-1]

    def implement(self, token, workspace, acceptance, probe, findings, round_index):
        @tool
        def read_source(path: str) -> str:
            """读取当前候选文件；path 为 . 时读取文件列表。"""
            files = self.backend.snapshot(workspace, workspace["task_id"])
            if path == ".":
                return canonical_json(list(files))
            source_path(path)
            if path not in files or files[path][0] == "120000":
                raise PermissionError("文件不存在或为链接")
            return files[path][1].decode("utf-8", "replace")

        @tool
        def write_source(path: str, content: str, delete: bool = False) -> str:
            """仅修改当前任务源码；不得改变已确认探针。"""
            if path in probe.files:
                raise PermissionError("不能修改已独审的探针材料")
            current = self.workspaces.require(token, workspace["id"], now=self.services.clock())
            if delete:
                self.backend.delete_source(current, workspace["task_id"], path)
            else:
                self.backend.write_source(current, workspace["task_id"], path, content.encode())
            return "当前任务修改已保存"

        def verify(result, events):
            actual = self.backend.snapshot(workspace, workspace["task_id"])
            return {
                "comparison": {"manifest": file_manifest(actual), "findings": findings},
                "verified_fact_ids": [],
            }

        self.runtime.run(
            token,
            StagePlan(
                "implement:" + str(round_index),
                "repair",
                "按当前批准范围修复，保留有效测试和检查，不弱化验收。验收与反馈："
                + canonical_json({"acceptance": acceptance, "findings": findings}),
                "implement",
                "processor",
                (
                    ToolBinding(read_source, BudgetAmounts(active_ms=30000)),
                    ToolBinding(write_source, BudgetAmounts(active_ms=30000)),
                ),
                verify,
                code_version=self.code(workspace),
                record_attempt=False,
            ),
        )

    def review(self, token, workspace, evidence, *, phase):
        child = Delegation(self.services.store).start(
            token, str(uuid4()), mode="review", role_key="reviewer", now=self.services.clock()
        )
        reads, decisions, check_requests = [], [], []

        @tool
        def read_evidence() -> str:
            """读取宿主保存的真实探针、两侧结果、差异与验收规范。"""
            reads.append(True)
            return canonical_json(evidence)

        @tool
        def request_check(argv: list[str], timeout: int = 60) -> str:
            """在隔离只读候选副本运行必要补查，不写源码。"""
            if not argv or not 0 < timeout <= 3600:
                raise ValueError("补查命令或超时无效")
            check_requests.append((argv, timeout))
            return "补查已登记；宿主将在模型阶段结束后串行执行并重新独审"

        def verify(result, events):
            if not reads:
                raise PermissionError("独审必须读取实际证据")
            decision = ReviewDecision.model_validate_json(result.summary)
            decisions.append(decision)
            return {"comparison": {"review": decision.model_dump()}, "verified_fact_ids": []}

        try:
            self.runtime.run(
                child,
                StagePlan(
                    "review:" + phase,
                    "repair-review",
                    "独立核验探针不夹带实现、目标有效性、回归和验收未被弱化。"
                    "完成后 StructuredResult.summary 必须为 ReviewDecision JSON，"
                    "含 passed、probe_valid、weakened_acceptance、findings、"
                    "unrelated_failures、rationale；不能凭成功退出码或模型自述通过。",
                    "review",
                    "reviewer",
                    (
                        ToolBinding(read_evidence, BudgetAmounts(active_ms=1000)),
                        ToolBinding(request_check, BudgetAmounts(active_ms=1000)),
                    ),
                    verify,
                    code_version=self.code(workspace),
                    record_attempt=False,
                ),
            )
            artifact = self.services.artifacts.register(
                child,
                "independent-review",
                decisions[-1].model_dump_json().encode(),
                context={
                    "producer": "independent_review",
                    "phase": phase,
                    "candidate_sha": workspace["sha"],
                },
                now=self.services.clock(),
            )
            additional = [
                self.check(child, workspace, argv, "review-check:" + uuid4().hex, timeout=timeout)
                for argv, timeout in check_requests
            ]
        finally:
            self.services.queue.complete(
                child, now=self.services.clock(), outcome="review_returned"
            )
        if check_requests:
            if phase.count(":supplement") >= 2:
                raise PermissionError("独审补查已达到有限上限，须明确处理")
            return self.review(
                token,
                workspace,
                {**evidence, "additional_checks": additional},
                phase=phase + ":supplement",
            )
        return {"decision": decisions[-1], "artifact_id": artifact, "run_id": child.run_id}

    def wait(self, token, reason):
        self.services.queue.complete(
            token, now=self.services.clock(), state="waiting", wait_reason=reason
        )
        return {"state": "waiting", "reason": reason}
