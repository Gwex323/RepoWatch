import json
from types import SimpleNamespace

import pytest

from repowatch.adapters.git_objects import GitObjects
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import ArtifactService
from repowatch.application.budgets import BudgetService
from repowatch.application.control import ControlService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.scans import ScanService
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords, block
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.scans import baseline_outcome, checks_for, inventory, readiness
from repowatch.domain.task_records import canonical_json
from repowatch.operations.migrate import migrate
from repowatch.runtime.git_handoff import create_fixture


@pytest.fixture
def scan_setup(tmp_path):
    fixture = create_fixture(tmp_path)
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repository = tasks.register_repository("scan-fixture", admission_mode="all_bugs")
    with store.write() as records:
        records.change("repositories", {"id": repository}, {"target_sha": fixture["commit"]})
    task = tasks.ensure_task(repository, "import:1", mode="scan", now=1)
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=1000000, model_calls=20, tokens=10000, repair_rounds=10, recovery_rounds=10
        ),
        expected_revision=1,
        reason="验收",
        now=1,
    )
    queue.set_pause(False, now=1)
    token = queue.claim(now=2, ttl=300)
    artifacts = ArtifactService(store, settings.artifacts_dir)
    objects = GitObjects(tmp_path / "objects")
    objects.import_bundle(repository, fixture["bundle"])
    scans = ScanService(store, objects, artifacts)
    yield SimpleNamespace(
        store=store,
        tasks=tasks,
        queue=queue,
        budgets=budgets,
        token=token,
        repository=repository,
        task=task,
        objects=objects,
        artifacts=artifacts,
        scans=scans,
        fixture=fixture,
    )
    store.close()


def test_inventory_uses_entire_git_tree_and_explicit_ranges(scan_setup):
    env = scan_setup
    manifest = inventory(env.objects, env.repository, env.fixture["commit"], chunk_bytes=256)
    assert {entry["path"] for entry in manifest["entries"]} == {
        entry["path"] for entry in env.fixture["entries"]
    }
    assert {entry["reason"] for entry in manifest["entries"] if entry["disposition"] == "gap"} == {
        "lfs_entity_missing",
        "submodule_content_missing",
    }
    for entry in manifest["entries"]:
        if entry["disposition"] == "review":
            units = [unit for unit in manifest["units"] if unit["path"] == entry["path"]]
            assert units[0]["start"] == 0 and units[-1]["end"] == entry["size"]
            assert all(
                left["end"] == right["start"] for left, right in zip(units, units[1:], strict=False)
            )


def test_model_completion_cannot_fill_coverage_or_external_content(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    gaps = env.scans.gaps(env.token, batch, now=3)
    unit = gaps["units"][0]
    inputs = env.scans.input(env.token, batch, unit["id"], now=3)
    with pytest.raises(PermissionError):
        env.scans.accept(env.token, batch, unit["id"], "invented", {}, now=3)
    assert not env.scans.accept(
        env.token, batch, unit["id"], inputs["artifact_id"], {"complete": True}, now=3
    )["accepted"]
    payload = {
        "input_sha256": unit["sha256"],
        "complete": True,
        "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
        "findings": [],
    }
    assert env.scans.accept(env.token, batch, unit["id"], inputs["artifact_id"], payload, now=3)[
        "accepted"
    ]
    assert env.scans.gaps(env.token, batch, now=3)["covered"] == 1
    with pytest.raises(PermissionError, match="缺口"):
        env.scans.validate_complete(env.token, batch, now=3)


def test_candidate_selection_is_local_idempotent_and_requires_issue_identity(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    unit = env.scans.gaps(env.token, batch, now=3)["units"][0]
    inputs = env.scans.input(env.token, batch, unit["id"], now=3)
    payload = {
        "input_sha256": unit["sha256"],
        "complete": True,
        "analysis": "已核对本块的输入及错误路径，保存为待核实的本地线索。",
        "findings": [
            {
                "title": "待核实边界",
                "basis": "此分块中的异常路径缺少处理",
                "start": unit["start"],
                "end": unit["end"],
            }
        ],
    }
    env.scans.accept(env.token, batch, unit["id"], inputs["artifact_id"], payload, now=3)
    with env.store.read() as records:
        candidate = records.find("scan_candidates")[0]
        assert not records.find("managed_pull_requests")
    result = env.scans.decide_candidate(
        "select", candidate["id"], "select", expected_revision=1, now=4
    )
    assert (
        env.scans.decide_candidate("select", candidate["id"], "select", expected_revision=1, now=4)
        == result
    )
    with env.store.read() as records:
        task = records.one("tasks", id=result["request_task_id"])
        assert task["issue_id"] is None and task["mode"] == "triage"
        assert json.loads(task["input_json"])["issue_lookup_required"]
        assert not records.find("managed_pull_requests")
    with pytest.raises(ConflictError):
        env.scans.decide_candidate("stale", candidate["id"], "exclude", expected_revision=1, now=4)


def test_import_epoch_and_policy_cannot_be_bypassed_by_sha_only(scan_setup):
    env = scan_setup
    with env.store.write() as records:
        records.change("repositories", {"id": env.repository}, {"identity_json": "{}"})
    with pytest.raises(PermissionError, match="覆盖账本"):
        env.queue.complete_scan(env.token, now=3, validated_sha=env.fixture["commit"])
    with env.store.read() as records:
        repository = records.one("repositories", id=env.repository)
        before = readiness(repository)
    with env.store.write() as records:
        records.change(
            "repositories", {"id": env.repository}, {"import_epoch": before["import_epoch"] + 1}
        )
    with env.store.read() as records:
        assert readiness(records.one("repositories", id=env.repository)) != before


@pytest.mark.parametrize(
    "code,log,expected",
    [
        (0, b"12 passed", "passed"),
        (1, b"FAILED tests/test_bug.py", "existing_failed"),
        (127, b"pytest: not found", "unable"),
        (1, b"No module named pytest", "unable"),
        (5, b"no tests ran", "unable"),
        (2, b"ERROR collecting tests", "unable"),
    ],
)
def test_baseline_classification(code, log, expected):
    result = {"exit_code": code, "timed_out": False, "oom_killed": False, "log_truncated": False}
    assert baseline_outcome(result, log) == expected


def test_hidden_ci_large_files_and_declared_checks_are_not_ignored():
    contents = {
        ".github/workflows/test.yml": b"name: CI",
        ".hidden": b"configuration",
        ".gitignore": b"src/",
        "src/large.py": b"value = 1\n" * 100,
        "package.json": b'{"scripts":{"test":"node --test","build":"tsc"}}',
    }

    class Objects:
        def entries(self, repository, sha):
            return [
                {"path": path, "oid": path, "mode": "100644", "kind": "blob"} for path in contents
            ]

        def blob(self, repository, oid):
            return contents[oid]

    objects = Objects()
    manifest = inventory(objects, "repo", "sha", chunk_bytes=256)
    assert len([unit for unit in manifest["units"] if unit["path"] == "src/large.py"]) == 4
    assert all(entry["disposition"] == "review" for entry in manifest["entries"])
    assert {check["id"] for check in checks_for(objects, "repo", manifest)} == {
        "js:test",
        "js:build",
    }
    assert canonical_json(manifest) == canonical_json(
        inventory(objects, "repo", "sha", chunk_bytes=256)
    )


def test_scan_attempt_replay_and_result_replay_do_not_duplicate_evidence(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    unit = env.scans.gaps(env.token, batch, now=3)["units"][0]
    inputs = env.scans.input(env.token, batch, unit["id"], now=3)
    payload = {
        "input_sha256": unit["sha256"],
        "complete": True,
        "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
        "findings": [],
    }
    first = env.scans.accept(env.token, batch, unit["id"], inputs["artifact_id"], payload, now=3)
    second = env.scans.accept(env.token, batch, unit["id"], inputs["artifact_id"], payload, now=3)
    assert first == second
    for _ in range(2):
        env.scans.record_attempt(
            env.token, batch, unit["id"], inputs["artifact_id"], "covered", now=3
        )
    assert len(env.scans.attempts(env.token, batch, now=3)) == 1


def test_manifest_policy_changes_create_distinct_batches(scan_setup):
    env = scan_setup
    first = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    second = env.scans.begin(
        env.token, env.fixture["commit"], {"image": "fixture"}, now=3, chunk_bytes=256
    )
    assert first != second


def test_control_stop_is_not_existing_code_failure():
    result = {
        "exit_code": 137,
        "timed_out": False,
        "oom_killed": False,
        "log_truncated": False,
        "control_stopped": True,
    }
    assert baseline_outcome(result, b"interrupted") == "unable"


def test_workspace_failed_rebuild_preserves_old_and_remove_retires_it(scan_setup):
    from repowatch.application.workspaces import WorkspaceService

    env = scan_setup
    removed = []
    result = {
        "ready": True,
        "id": "old",
        "sha": env.fixture["commit"],
        "image": "fixture",
    }
    backend = SimpleNamespace(create=lambda *args, **kwargs: result, remove=removed.append)
    workspaces = WorkspaceService(env.store, backend)
    workspaces.create(env.token, env.fixture["commit"], now=3)
    result = {"ready": False, "gaps": [{"reason": "missing_environment"}]}
    assert not workspaces.rebuild(env.token, "old", now=3)["ready"]
    assert workspaces.require(env.token, "old", now=3)["ready"]
    assert not removed
    workspaces.remove(env.token, "old", now=3)
    assert len(removed) == 1
    with pytest.raises(PermissionError):
        workspaces.require(env.token, "old", now=3)


def test_scan_coordinator_rejects_fetched_drift_before_workspace_creation(scan_setup, monkeypatch):
    from repowatch.application.repositories import RepositoryService
    from repowatch.worker.scans import ScanCoordinator

    env = scan_setup
    with env.store.write() as records:
        records.change(
            "repositories",
            {"id": env.repository},
            {
                "identity_json": "{}",
                "github_owner": "up",
                "github_name": "repo",
                "target_branch": "main",
                "credential_reference": "github",
            },
        )
    services = SimpleNamespace(
        store=env.store,
        queue=env.queue,
        budgets=env.budgets,
        clock=lambda: 3,
        vault=SimpleNamespace(read=lambda _: None),
        repositories=RepositoryService(
            env.store, lambda _: SimpleNamespace(branch=lambda *args: env.fixture["commit"])
        ),
    )
    monkeypatch.setattr(
        "repowatch.worker.scans.BudgetedExecutor.execute",
        lambda self, token, key, amounts, action, usage, **kwargs: action(),
    )
    coordinator = object.__new__(ScanCoordinator)
    coordinator.services = services
    coordinator.objects = SimpleNamespace(fetch=lambda *args, **kwargs: "b" * 40)
    result = coordinator.run(env.token)
    assert result["reason"] == "target_drift"
    with env.store.read() as records:
        assert not records.find("workspaces")
        assert records.one("repositories", id=env.repository)["scan_ready_sha"] is None
        scans = records.find("tasks", mode="scan")
        assert len(scans) == 2
        assert all(task["budget_owner_task_id"] == env.task for task in scans)


def test_target_read_failure_is_persistent_and_revokes_old_readiness(scan_setup):
    from repowatch.application.repositories import RepositoryService

    env = scan_setup
    with env.store.write() as records:
        records.change(
            "repositories",
            {"id": env.repository},
            {
                "scan_ready_sha": env.fixture["commit"],
                "scan_ready_json": "{}",
            },
        )

    def fail(*args):
        raise OSError("network unavailable")

    service = RepositoryService(env.store, lambda _: SimpleNamespace(branch=fail))
    with pytest.raises(PermissionError, match="目标分支无法读取"):
        service.verify_target(env.token, now=3)
    with env.store.read() as records:
        assert records.one("repositories", id=env.repository)["scan_ready_sha"] is None
        assert records.find("block_reasons", code="target_read_failed", resolved=0)
        assert records.one("global_control", singleton=1)["paused"]


def test_no_declared_checks_records_host_not_applicable_evidence(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    checks = env.scans.gaps(env.token, batch, now=3)["checks"]
    assert checks[0]["applicability"] == "not_applicable"
    env.scans.baseline(env.token, batch, checks[0]["id"], None, "not_applicable", now=3)
    assert not env.scans.gaps(env.token, batch, now=3)["checks"]
    view = env.scans.view(env.repository)["batches"][0]
    assert view["baselines"][0]["evidence"]["basis"]
    first_id = view["baselines"][0]["evidence"]["log_artifact_id"]
    artifact = env.artifacts.require(first_id, now=3)
    (env.artifacts.root / artifact["relative_path"]).unlink()
    assert env.scans.gaps(env.token, batch, now=3)["checks"]
    env.scans.baseline(env.token, batch, checks[0]["id"], None, "not_applicable", now=3)
    assert not env.scans.gaps(env.token, batch, now=3)["checks"]
    with env.store.read() as records:
        assert not records.one("global_control", singleton=1)["paused"]


def test_scan_view_does_not_count_missing_result(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    unit = env.scans.gaps(env.token, batch, now=3)["units"][0]
    inputs = env.scans.input(env.token, batch, unit["id"], now=3)
    result = env.scans.accept(
        env.token,
        batch,
        unit["id"],
        inputs["artifact_id"],
        {
            "input_sha256": unit["sha256"],
            "complete": True,
            "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
            "findings": [],
        },
        now=3,
    )
    artifact = env.artifacts.require(result["artifact_id"], now=3)
    (env.artifacts.root / artifact["relative_path"]).unlink()
    view = env.scans.view(env.repository)["batches"][0]
    assert len(view["recorded_coverage"]) == 1
    assert not view["coverage"] and not view["evidence_valid"]


def test_requirements_check_and_locked_offline_dependency_plan():
    from repowatch.domain.dependencies import dependency_plan

    contents = {"requirements.txt": b"pytest==8.4.0\n"}
    objects = SimpleNamespace(
        entries=lambda *args: [
            {"path": path, "oid": path, "mode": "100644", "kind": "blob"} for path in contents
        ],
        blob=lambda repository, oid: contents[oid],
    )
    manifest = inventory(objects, "repo", "sha")
    assert checks_for(objects, "repo", manifest)[0]["id"] == "python:pytest"
    plan = dependency_plan(objects, "repo", manifest)
    assert "--no-index" in plan["steps"][0]
    assert "--target=/deps/python" in plan["steps"][0]
    contents["requirements.txt"] = b"https://user:password@example.org/private.whl"
    assert dependency_plan(objects, "repo", manifest)["gaps"][0]["reason"].startswith("private_")


@pytest.mark.parametrize(
    "contents,expected_step,expected_gap",
    [
        (
            {"requirements-dev.txt": b"pytest==8.4.0\n"},
            "requirements-dev.txt",
            None,
        ),
        (
            {
                "pyproject.toml": b'[project]\ndependencies = ["requests"]\n',
            },
            None,
            "locked_dependency_plan_required",
        ),
        (
            {
                "pyproject.toml": b'[project]\ndependencies = ["requests"]\n',
                "requirements.txt": b"requests==2.32.0\n",
            },
            "requirements.txt",
            "locked_dependency_plan_required",
        ),
        (
            {
                "pyproject.toml": b'[project]\ndependencies = ["requests"]\n',
                "uv.lock": b"version = 1\n",
            },
            "uv",
            None,
        ),
        (
            {
                "packages/api/package.json": b'{"dependencies":{"left-pad":"1"}}',
                "packages/api/package-lock.json": b'{"lockfileVersion":3}',
            },
            None,
            "unsupported_nested_dependency_project",
        ),
        (
            {
                "package.json": b'{"workspaces":["packages/*"],"dependencies":{"x":"1"}}',
                "package-lock.json": b'{"lockfileVersion":3}',
                "packages/api/package.json": b'{"dependencies":{"left-pad":"1"}}',
            },
            None,
            "unsupported_workspace_layout",
        ),
        (
            {
                "package.json": b'{"dependencies":{"fixturepkg":"file:vendor/pkg"}}',
                "package-lock.json": b'{"lockfileVersion":3}',
                "vendor/pkg/package.json": b'{"name":"fixturepkg","version":"1.0.0"}',
            },
            "npm",
            None,
        ),
        (
            {"package-lock.json": b'{"lockfileVersion":3}'},
            None,
            "dependency_manifest_missing",
        ),
    ],
)
def test_dependency_plan_reports_unsupported_layouts_and_uses_declared_lockfiles(
    contents, expected_step, expected_gap
):
    from hashlib import sha256

    from repowatch.domain.dependencies import dependency_plan

    objects = SimpleNamespace(
        entries=lambda *args: [
            {"path": path, "oid": path, "mode": "100644", "kind": "blob"} for path in contents
        ],
        blob=lambda repository, oid: contents[oid],
    )
    manifest = inventory(objects, "repo", "sha")
    plan = dependency_plan(objects, "repo", manifest)
    if expected_step in {"requirements.txt", "requirements-dev.txt"}:
        assert plan["steps"] == [
            [
                "python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links=/opt/repowatch/wheels",
                "--target=/deps/python",
                "-r",
                expected_step,
            ]
        ]
    elif expected_step:
        assert plan["steps"][0][0] == expected_step
    else:
        assert not plan["steps"]
    if expected_gap:
        assert any(gap["reason"] == expected_gap for gap in plan["gaps"])
    else:
        assert not plan["gaps"]
    assert {item["path"] for item in plan["declarations"]} == set(contents)
    assert {item["sha256"] for item in plan["declarations"]} == {
        sha256(content).hexdigest() for content in contents.values()
    }


def test_dependency_preparation_report_parser_preserves_host_gap_details():
    from repowatch.worker.runner import preparation_report_from_log

    report = {
        "reason": "dependency_preparation_unavailable",
        "gaps": [{"path": "pyproject.toml", "reason": "locked_dependency_plan_required"}],
        "steps": [],
    }
    log = (
        b"starting baseline\nREPOWATCH_ENVIRONMENT_UNAVAILABLE: " + canonical_json(report).encode()
    )
    assert preparation_report_from_log(log) == report
    assert preparation_report_from_log(b"no environment marker") is None


@pytest.mark.parametrize(
    "exit_code,log,outcome",
    [
        (0, b"1 passed", "passed"),
        (1, b"FAILED tests/test_existing_bug.py", "existing_failed"),
        (125, b"REPOWATCH_ENVIRONMENT_UNAVAILABLE", "unable"),
    ],
)
def test_baseline_persists_only_matching_runner_evidence(scan_setup, exit_code, log, outcome):
    env = scan_setup
    package_files = {
        "package.json": b'{"scripts":{"test":"node --test"}}',
        "package-lock.json": b'{"lockfileVersion":3}',
    }

    class Objects:
        def entries(self, repository_id, sha):
            return [
                {"path": path, "oid": path, "mode": "100644", "kind": "blob"}
                for path in package_files
            ]

        def blob(self, repository_id, oid):
            return package_files[oid]

    env.scans.objects = Objects()
    batch_id = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture-image"}, now=3)
    check = env.scans.gaps(env.token, batch_id, now=3)["checks"][0]
    workspace = {
        "id": "baseline-workspace",
        "task_id": env.task,
        "repository_id": env.repository,
        "sha": env.fixture["commit"],
        "image": "fixture-image",
        "manifest_sha256": "a" * 64,
    }
    command_id = "baseline-command"
    log_id = env.artifacts.register(
        env.token,
        "baseline-command-log",
        log,
        context={"producer": "docker_runner", "sha": env.fixture["commit"]},
        now=3,
    )
    result = {
        "command_id": command_id,
        "workspace_id": workspace["id"],
        "argv": check["argv"],
        "sha": env.fixture["commit"],
        "image": "fixture-image",
        "exit_code": exit_code,
        "timed_out": False,
        "oom_killed": False,
        "log_truncated": False,
        "control_stopped": False,
        "log_artifact_id": log_id,
        "preparation_report": (
            {"reason": "dependency_install_failed", "step": ["npm", "ci"]}
            if outcome == "unable"
            else None
        ),
    }
    with env.store.write() as records:
        records.add(
            "workspaces",
            {
                "id": workspace["id"],
                "task_id": env.task,
                "repository_id": env.repository,
                "snapshot_json": canonical_json(workspace),
                "state": "ready",
                "created_at": 3,
            },
        )
        records.add(
            "command_states",
            {
                "id": command_id,
                "run_id": env.token.run_id,
                "generation": env.token.generation,
                "state": "stopped",
                "identity_json": canonical_json({"container": "runner", "argv": check["argv"]}),
                "evidence_json": "{}",
            },
        )
        records.add(
            "runner_results",
            {
                "command_id": command_id,
                "run_id": env.token.run_id,
                "workspace_id": workspace["id"],
                "result_json": canonical_json(result),
                "artifact_id": log_id,
                "created_at": 3,
            },
        )
    env.scans.baseline(env.token, batch_id, check["id"], result, outcome, now=3)
    with env.store.read() as records:
        baseline = dict(records.one("scan_baselines", batch_id=batch_id, check_id=check["id"]))
    assert baseline["outcome"] == outcome
    evidence = json.loads(baseline["evidence_json"])
    assert evidence["argv"] == check["argv"]
    assert evidence["log_artifact_id"] == log_id
    assert evidence["preparation_report"] == result["preparation_report"]
    gaps = env.scans.gaps(env.token, batch_id, now=3)
    assert (check["id"] in {item["id"] for item in gaps["checks"]}) == (outcome == "unable")


def test_scan_completion_wakes_reimported_running_repair(scan_setup):
    from repowatch.application.repositories import RepositoryService
    from repowatch.application.task_records import create_task
    from repowatch.domain.task_records import stable_id

    env = scan_setup
    env.queue.complete(env.token, now=3)
    with env.store.write() as records:
        records.change(
            "repositories",
            {"id": env.repository},
            {
                "target_sha": "a" * 40,
                "scan_ready_sha": "a" * 40,
                "target_branch": "main",
                "publication_json": "{}",
                "contribution_path": "direct",
            },
        )
        repair_id = create_task(records, env.repository, "issue:44", "44", "implement", 3)
        records.change(
            "budget_ledgers",
            {"owner_task_id": repair_id},
            {
                "limits_json": canonical_json(
                    {
                        "active_ms": 1000000,
                        "tokens": 10000,
                        "model_calls": 20,
                        "repair_rounds": 10,
                        "recovery_rounds": 10,
                    }
                ),
            },
        )
    repair_token = env.queue.claim(now=4, ttl=300)
    with env.store.write() as records:
        preflight_id = stable_id("test-preflight", env.repository)
        records.add(
            "user_operations",
            {
                "id": "test-preflight",
                "request_sha256": "a" * 64,
                "expected_revision": 1,
                "result_json": "{}",
            },
        )
        records.add(
            "repository_preflights",
            {
                "id": preflight_id,
                "operation_id": "test-preflight",
                "request_json": '{"credential_reference":"github"}',
                "identity_json": canonical_json(
                    {
                        "github_id": "scan-fixture",
                        "owner": "up",
                        "name": "repo",
                        "default_branch": "main",
                        "target_branch": "main",
                        "target_sha": "a" * 40,
                        "maintained": True,
                        "publication": {},
                        "contribution_path": "direct",
                    }
                ),
                "created_at": 4,
                "expires_at": 100,
            },
        )
    service = RepositoryService(env.store, None)
    imported = service.save("reimport", preflight_id, {}, expected_revision=1, now=5)
    env.queue.complete(repair_token, now=6)
    with env.store.read() as records:
        repair = records.one("tasks", id=repair_id)
        assert repair["state"] == "waiting" and repair["wait_reason"] == "scan"
    with env.store.write() as records:
        records.change("repositories", {"id": env.repository}, {"identity_json": None})
        records.change(
            "budget_ledgers",
            {"owner_task_id": imported["scan_task_id"]},
            {
                "limits_json": canonical_json(
                    {
                        "active_ms": 1000000,
                        "tokens": 10000,
                        "model_calls": 20,
                        "repair_rounds": 10,
                        "recovery_rounds": 10,
                    }
                ),
            },
        )
    scan_token = env.queue.claim(now=7, ttl=300)
    env.queue.complete_scan(scan_token, now=8, validated_sha="a" * 40)
    with env.store.read() as records:
        repair = records.one("tasks", id=repair_id)
        assert repair["state"] == "queued" and repair["stop_requested"] == 0
        assert not records.find("scan_dependencies", waiting_task_id=repair_id)


def test_split_preserves_exact_ranges_and_requires_all_child_evidence(scan_setup):
    env = scan_setup
    content = b"value = 1\n" * 100
    env.scans.objects = SimpleNamespace(
        entries=lambda *args: [
            {
                "path": "large.py",
                "oid": "a" * 40,
                "mode": "100644",
                "kind": "blob",
            }
        ],
        blob=lambda *args: content,
    )
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    parent = env.scans.gaps(env.token, batch, now=3)["units"][0]
    children = env.scans.split(env.token, batch, parent["id"], now=3)
    assert children[0]["start"] == 0
    assert children[0]["end"] == children[1]["start"]
    assert children[1]["end"] == len(content)
    assert env.scans.split(env.token, batch, parent["id"], now=3) == children
    first = children[0]
    inputs = env.scans.input(env.token, batch, first["id"], now=3)
    env.scans.accept(
        env.token,
        batch,
        first["id"],
        inputs["artifact_id"],
        {
            "input_sha256": first["sha256"],
            "complete": True,
            "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
            "findings": [],
        },
        now=3,
    )
    gaps = env.scans.gaps(env.token, batch, now=3)
    assert gaps["total"] == 2 and gaps["covered"] == 1
    assert gaps["units"] == children[1:]
    with pytest.raises(PermissionError, match="缺口"):
        env.scans.validate_complete(env.token, batch, now=3)


def test_environment_change_rejects_complete_even_without_content_gaps(scan_setup):
    env = scan_setup
    env.scans.objects = SimpleNamespace(entries=lambda *args: [], blob=lambda *args: b"")
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    check = env.scans.gaps(env.token, batch, now=3)["checks"][0]
    env.scans.baseline(env.token, batch, check["id"], None, "not_applicable", now=3)
    env.scans.validate_complete(env.token, batch, now=3)
    with env.store.write() as records:
        records.change(
            "tasks",
            {"id": env.task},
            {
                "configuration_json": '{"workspace_image":"changed"}',
            },
        )
    with pytest.raises(ConflictError, match="环境或工具配置变化"):
        env.scans.validate_complete(env.token, batch, now=3)


def test_new_environment_batch_prevents_old_environment_completion(scan_setup):
    env = scan_setup
    env.scans.objects = SimpleNamespace(entries=lambda *args: [], blob=lambda *args: b"")
    first = env.scans.begin(env.token, env.fixture["commit"], {"image": "old"}, now=3)
    check = env.scans.gaps(env.token, first, now=3)["checks"][0]
    env.scans.baseline(env.token, first, check["id"], None, "not_applicable", now=3)
    second = env.scans.begin(env.token, env.fixture["commit"], {"image": "new"}, now=3)
    assert first != second
    with pytest.raises(ConflictError, match="环境或策略批次替代"):
        env.scans.validate_complete(env.token, first, now=3)


@pytest.mark.parametrize("scenario", ["complete", "failed_rebuild", "late_drift", "truncated"])
def test_scan_coordinator_completes_and_retires_workspace(scan_setup, monkeypatch, scenario):
    from repowatch.application.repositories import RepositoryService
    from repowatch.application.workspaces import WorkspaceService
    from repowatch.worker.scans import ScanCoordinator

    env = scan_setup
    with env.store.write() as records:
        records.change("repositories", {"id": env.repository}, {"identity_json": "{}"})
    content = b"value = 1\n" * 100
    objects = SimpleNamespace(
        fetch=lambda *args, **kwargs: env.fixture["commit"],
        entries=lambda *args: [
            {
                "path": "large.py",
                "oid": "a" * 40,
                "mode": "100644",
                "kind": "blob",
            }
        ],
        blob=lambda *args: content,
        repository=lambda *args: "fixture",
        run=lambda *args: b"c" * 40,
    )
    backend = SimpleNamespace(
        create=lambda *args, **kwargs: {
            "id": "workspace",
            "ready": True,
            "repository_id": env.repository,
            "sha": env.fixture["commit"],
            "image": "fixture",
            "manifest_sha256": "a" * 64,
        },
        remove=lambda *args: None,
    )
    observed = [env.fixture["commit"]]
    services = SimpleNamespace(
        store=env.store,
        queue=env.queue,
        budgets=env.budgets,
        artifacts=env.artifacts,
        clock=lambda: 3,
        vault=SimpleNamespace(read=lambda _: None),
        repositories=RepositoryService(
            env.store, lambda _: SimpleNamespace(branch=lambda *args: observed[0])
        ),
    )
    monkeypatch.setattr(
        "repowatch.worker.scans.BudgetedExecutor.execute",
        lambda self, token, key, amounts, action, usage, **kwargs: action(),
    )
    coordinator = object.__new__(ScanCoordinator)
    coordinator.services, coordinator.objects, coordinator.backend = services, objects, backend
    coordinator.scans = ScanService(env.store, objects, env.artifacts)
    coordinator.workspaces = WorkspaceService(env.store, backend)
    calls = []
    if scenario == "failed_rebuild":
        original_begin = coordinator.scans.begin

        def with_check(*args, **kwargs):
            batch_id = original_begin(*args, **kwargs)
            with env.store.write() as records:
                records.change(
                    "scan_batches",
                    {"id": batch_id},
                    {
                        "checks_json": canonical_json(
                            [
                                {
                                    "id": "fixture",
                                    "argv": ["false"],
                                    "basis": "fixture",
                                }
                            ]
                        ),
                    },
                )
            return batch_id

        monkeypatch.setattr(coordinator.scans, "begin", with_check)
        monkeypatch.setattr(coordinator.scans, "baseline", lambda *args, **kwargs: None)
        artifact_id = env.artifacts.register(
            env.token, "fixture-log", b"missing dependency", context={}, now=3
        )
        coordinator.runner = SimpleNamespace(
            check=lambda *args, **kwargs: {
                "log_artifact_id": artifact_id,
                "exit_code": 125,
                "timed_out": False,
                "oom_killed": False,
                "log_truncated": False,
            }
        )
        backend.create = lambda *args, **kwargs: {"ready": False, "gaps": []}

        def initial_create(*args, **kwargs):
            result = {
                "id": "workspace",
                "ready": True,
                "repository_id": env.repository,
                "sha": env.fixture["commit"],
                "image": "fixture",
                "manifest_sha256": "a" * 64,
            }
            with env.store.write() as records:
                records.add(
                    "workspaces",
                    {
                        "id": "workspace",
                        "task_id": env.task,
                        "repository_id": env.repository,
                        "snapshot_json": canonical_json(result),
                        "state": "ready",
                        "created_at": 3,
                    },
                )
            monkeypatch.setattr(coordinator.workspaces, "create", original_create)
            return result

        original_create = coordinator.workspaces.create
        monkeypatch.setattr(coordinator.workspaces, "create", initial_create)

    def review(token, plan):
        inputs = json.loads(plan.tools[0].tool.invoke({}))
        unit = inputs["unit"]
        payload = {
            "input_sha256": unit["sha256"],
            "complete": len(calls) != 0,
            "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
            "findings": [],
        }
        calls.append(unit)
        if scenario == "truncated" and len(calls) == 1:
            from repowatch.runtime.prototype import MissingStructuredResult

            raise MissingStructuredResult()
        plan.tools[1].tool.invoke(payload)
        if scenario == "late_drift" and len(calls) == 3:
            observed[0] = "b" * 40
        return SimpleNamespace(status="completed")

    coordinator.runtime = SimpleNamespace(run=review)
    if scenario == "failed_rebuild":
        with pytest.raises(PermissionError, match="重建预检失败"):
            coordinator.run(env.token)
        with env.store.read() as records:
            assert records.one("workspaces", id="workspace")["state"] == "ready"
            assert records.one("repositories", id=env.repository)["scan_ready_json"] is None
        return
    result = coordinator.run(env.token)
    assert result["state"] == "ended"
    assert len(calls) == 3
    with env.store.read() as records:
        assert records.one("workspaces", id="workspace")["state"] == "removed"
        if scenario == "late_drift":
            assert result["reason"] == "target_drift"
            assert records.one("repositories", id=env.repository)["scan_ready_json"] is None
            assert all(row["state"] != "completed" for row in records.find("scan_batches"))
            return
        assert records.one("scan_batches", id=result["batch_id"])["state"] == "completed"
        assert json.loads(records.one("repositories", id=env.repository)["scan_ready_json"])
        assert (
            records.one("repositories", id=env.repository)["scan_ready_batch_id"]
            == result["batch_id"]
        )
        task_revision = records.one("tasks", id=env.task)["revision"]
    env.tasks.operate(
        "change-environment",
        env.task,
        expected_revision=task_revision,
        action="update_configuration",
        payload={"workspace_image": "new-environment"},
        now=4,
    )
    with env.store.read() as records:
        assert records.one("repositories", id=env.repository)["scan_ready_json"] is None


def test_scan_information_and_reenable_do_not_change_mode(scan_setup):
    env = scan_setup
    env.queue.complete(env.token, now=3, state="waiting", wait_reason="human_information")
    with env.store.read() as records:
        task = records.one("tasks", id=env.task)
    env.tasks.operate(
        "scan-info",
        env.task,
        expected_revision=task["revision"],
        action="supply_information",
        payload={"environment": "offline fixture"},
        now=4,
    )
    with env.store.read() as records:
        task = records.one("tasks", id=env.task)
        assert task["mode"] == "scan"
    env.tasks.operate(
        "scan-cancel", env.task, expected_revision=task["revision"], action="cancel", now=5
    )
    with env.store.read() as records:
        task = records.one("tasks", id=env.task)
    env.tasks.operate(
        "scan-reenable", env.task, expected_revision=task["revision"], action="reenable", now=6
    )
    with env.store.read() as records:
        assert records.one("tasks", id=env.task)["mode"] == "scan"


def test_missing_scan_evidence_is_recollected_without_global_pause(scan_setup):
    env = scan_setup
    batch = env.scans.begin(env.token, env.fixture["commit"], {"image": "fixture"}, now=3)
    unit = env.scans.gaps(env.token, batch, now=3)["units"][0]
    inputs = env.scans.input(env.token, batch, unit["id"], now=3)
    payload = {
        "input_sha256": unit["sha256"],
        "complete": True,
        "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
        "findings": [],
    }
    result = env.scans.accept(env.token, batch, unit["id"], inputs["artifact_id"], payload, now=3)
    for artifact_id in (inputs["artifact_id"], result["artifact_id"]):
        artifact = env.artifacts.require(artifact_id, now=3)
        (env.artifacts.root / artifact["relative_path"]).unlink()
    assert env.scans.gaps(env.token, batch, now=3)["covered"] == 0
    replacement = env.scans.input(env.token, batch, unit["id"], now=3)
    assert replacement["artifact_id"] != inputs["artifact_id"]
    env.scans.accept(env.token, batch, unit["id"], replacement["artifact_id"], payload, now=3)
    assert env.scans.gaps(env.token, batch, now=3)["covered"] == 1
    with env.store.read() as records:
        assert not records.one("global_control", singleton=1)["paused"]
        assert not records.find("block_reasons", resolved=0)


def test_publish_guard_rejects_old_epoch_even_when_sha_matches(scan_setup):
    env = scan_setup
    reservation = env.budgets.reserve(
        env.token, "publish-gate", BudgetAmounts(active_ms=1000), purpose="publish", now=3
    )
    env.budgets.start(env.token, reservation.reservation_id, now=3)
    with env.store.write() as records:
        repository = records.one("repositories", id=env.repository)
        old_version = readiness(repository)
        records.change(
            "repositories",
            {"id": env.repository},
            {
                "identity_json": "{}",
                "scan_ready_sha": env.fixture["commit"],
                "scan_ready_json": canonical_json(old_version),
                "import_epoch": repository["import_epoch"] + 1,
            },
        )
    with pytest.raises(PermissionError):
        env.queue.guard(
            env.token, now=3, purpose="publish", reservation_id=reservation.reservation_id
        )


def test_publish_guard_always_calls_target_gate_for_imported_repository(scan_setup):
    env = scan_setup
    calls = []
    with env.store.write() as records:
        records.change("repositories", {"id": env.repository}, {"identity_json": "{}"})
    env.queue.target_gate = lambda token, **kwargs: calls.append(token) or False
    with pytest.raises(PermissionError, match="发布必须无条件读取"):
        env.queue.guard(env.token, now=3, purpose="publish")
    assert calls == [env.token]


def test_ready_gate_releases_slot_and_reuses_drift_scan(scan_setup):
    from repowatch.application.repositories import RepositoryService

    env = scan_setup
    service = RepositoryService(env.store, lambda _: SimpleNamespace(branch=lambda *args: "b" * 40))
    assert not service.require_ready(env.token, now=3)
    with env.store.read() as records:
        scans = [task for task in records.find("tasks", mode="scan") if task["id"] != env.task]
        assert len(scans) == 1
        assert scans[0]["budget_owner_task_id"] == env.task
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
        assert records.one("tasks", id=env.task)["wait_reason"] == "scan"
        assert records.one(
            "scan_dependencies", waiting_task_id=env.task, scan_task_id=scans[0]["id"]
        )


def test_rebuild_rounds_use_original_budget_and_do_not_create_progress(scan_setup):
    from repowatch.application.budgets import BudgetedExecutor
    from repowatch.application.workspaces import WorkspaceService
    from repowatch.worker.scans import ScanCoordinator

    env = scan_setup
    created = []

    def create(*args, **kwargs):
        identifier = "rebuild-" + str(len(created))
        created.append(identifier)
        return {
            "id": identifier,
            "ready": True,
            "sha": env.fixture["commit"],
            "image": "fixture",
        }

    backend = SimpleNamespace(create=create, remove=lambda *args: None)
    coordinator = object.__new__(ScanCoordinator)
    coordinator.services = SimpleNamespace(clock=lambda: 3)
    coordinator.workspaces = WorkspaceService(env.store, backend)
    workspace = coordinator.workspaces.create(env.token, env.fixture["commit"], now=3)
    executor = BudgetedExecutor(env.budgets, env.queue, clock=lambda: 3)
    for _ in range(2):
        workspace = coordinator.rebuild(env.token, workspace, "check", executor)
    budget = env.budgets.view(env.task)
    assert budget["used"]["recovery_rounds"] == 2
    assert budget["used"]["model_calls"] == 0
    with env.store.read() as records:
        assert not records.find("budget_progress", owner_task_id=env.task)
        reservations = records.find("budget_reservations", owner_task_id=env.task)
        assert len(reservations) == 4
        assert all(row["state"] == "settled" for row in reservations)
        assert len(records.find("business_events", kind="workspace_preflight")) == 3


def test_consecutive_rebuild_failures_require_human_plan_and_explicit_resume(scan_setup):
    from repowatch.application.budgets import BudgetedExecutor
    from repowatch.application.workspaces import WorkspaceService
    from repowatch.worker.scans import ScanCoordinator

    env = scan_setup
    ready = {"value": True}
    clock = {"now": 3}

    def create(*args, **kwargs):
        return {
            "id": "recovery-workspace-" + str(len(created)),
            "ready": ready["value"],
            "sha": env.fixture["commit"],
            "image": "fixture",
            "gaps": [] if ready["value"] else [{"reason": "dependency_missing"}],
        }

    created = []

    def tracked_create(*args, **kwargs):
        result = create(*args, **kwargs)
        created.append(result["id"])
        return result

    backend = SimpleNamespace(create=tracked_create, remove=lambda _: None)
    workspaces = WorkspaceService(env.store, backend)
    workspace = workspaces.create(env.token, env.fixture["commit"], now=3)
    executor = BudgetedExecutor(env.budgets, env.queue, clock=lambda: clock["now"])
    coordinator = object.__new__(ScanCoordinator)
    coordinator.services = SimpleNamespace(clock=lambda: clock["now"])
    coordinator.workspaces = workspaces

    ready["value"] = False
    first_replacement = coordinator.rebuild(env.token, workspace, "pytest", executor)
    assert not first_replacement["ready"]
    clock["now"] = 4
    env.queue.complete(env.token, now=4, state="queued")
    clock["now"] = 5
    second_token = env.queue.claim(now=5, ttl=300)
    second_replacement = coordinator.rebuild(second_token, workspace, "pytest", executor)
    assert not second_replacement["ready"]
    clock["now"] = 6
    with env.store.write() as records:
        block(records, "scan_safety_boundary", env.task, now=6, detail="dependency_missing")
        reason = dict(records.find("block_reasons", task_id=env.task, resolved=0)[0])
    env.queue.complete(second_token, now=6, state="blocked", outcome="scan_safety_boundary")

    with env.store.read() as records:
        task_revision = records.one("tasks", id=env.task)["revision"]
    clock["now"] = 7
    env.tasks.operate(
        "recovery-information",
        env.task,
        expected_revision=task_revision,
        action="supply_information",
        payload={"environment": "offline wheels are available in the selected image"},
        now=7,
    )
    with env.store.read() as records:
        updated_task = records.one("tasks", id=env.task)
        assert (
            json.loads(updated_task["input_json"])["human_context"]["environment"]
            == "offline wheels are available in the selected image"
        )
    control = ControlService(env.store)
    clock["now"] = 8
    resolved = control.resolve_reason(
        "recovery-plan",
        reason["id"],
        expected_revision=reason["revision"],
        explanation="确认缺失依赖并使用含离线 wheel 的固定镜像重试",
        retry_plan={
            "steps": ["使用已配置离线 wheel 镜像重建工作区", "重新运行 pytest 基线"],
            "success_condition": "依赖成功安装且 pytest 返回实际结果",
        },
        retry_limits={"max_attempts": 1, "max_steps": 4, "max_active_ms": 120000},
        now=8,
    )
    assert resolved["resolved"] and resolved["global_paused"]
    assert resolved["retry_authorization_id"]

    with env.store.read() as records:
        control_revision = records.one("global_control", singleton=1)["revision"]
    control.resume(
        "recovery-resume",
        expected_revision=control_revision,
        now=9,
    )
    clock["now"] = 9
    retry_token = env.queue.claim(now=10, ttl=300)
    assert retry_token is not None
    with env.store.read() as records:
        retry_snapshot = json.loads(records.one("runs", id=retry_token.run_id)["snapshot_json"])
        assert retry_snapshot["retry_authorization"]["id"] == resolved["retry_authorization_id"]
        assert records.one("block_reasons", id=reason["id"])["resolved"] == 1
        assert records.one("tasks", id=env.task)["input_revision"] == 2

    ready["value"] = True
    clock["now"] = 10
    rebuilt = coordinator.rebuild(retry_token, workspace, "pytest", executor)
    assert rebuilt["ready"]
    env.queue.complete(retry_token, now=11, state="queued")
    budget = env.budgets.view(env.task)
    assert budget["used"]["recovery_rounds"] == 3
    with env.store.read() as records:
        assert records.one("workspaces", id=workspace["id"])["state"] == "removed"
        assert len(records.find("business_events", kind="workspace_preflight")) == 4
        assert not records.find("budget_progress", owner_task_id=env.task)


def test_worker_reclaims_scan_after_repeated_rebuild_failures_and_wakes_waiter(
    scan_setup, monkeypatch
):
    from repowatch.adapters.credentials import SecretValue
    from repowatch.application.budgets import BudgetedExecutor
    from repowatch.application.workspaces import WorkspaceService
    from repowatch.worker.scans import ScanCoordinator
    from repowatch.worker.service import Worker

    env = scan_setup
    clock = {"now": 3}
    environment = {"ready": True}
    created = []
    env.scans.objects = SimpleNamespace(entries=lambda *args: [], blob=lambda *args: b"")
    with env.store.write() as records:
        records.change(
            "repositories",
            {"id": env.repository},
            {
                "identity_json": "{}",
                "github_owner": "fixture",
                "github_name": "worker-recovery",
                "target_branch": "main",
                "credential_reference": "fixture-credential",
            },
        )

    def create(repository_id, sha, *, task_id, mode, image):
        identifier = "worker-recovery-" + str(len(created))
        created.append(identifier)
        return {
            "id": identifier,
            "ready": environment["ready"],
            "repository_id": repository_id,
            "task_id": task_id,
            "mode": mode,
            "sha": sha,
            "image": image,
            "manifest_sha256": "a" * 64,
            "gaps": [] if environment["ready"] else [{"reason": "dependency_missing"}],
        }

    backend = SimpleNamespace(create=create, remove=lambda _: None)
    workspaces = WorkspaceService(env.store, backend)
    env.queue.complete(env.token, now=3, state="queued")
    waiter = env.tasks.ensure_task(
        env.repository, "issue:worker-recovery", issue_id="worker-recovery", now=3
    )
    with env.store.write() as records:
        records.change("tasks", {"id": waiter}, {"state": "waiting", "wait_reason": "scan"})
        records.add("scan_dependencies", {"waiting_task_id": waiter, "scan_task_id": env.task})
    services = SimpleNamespace(
        store=env.store,
        queue=env.queue,
        budgets=env.budgets,
        artifacts=env.artifacts,
        clock=lambda: clock["now"],
        diagnostics=SimpleNamespace(expire_queued=lambda **_: None),
        settings=SimpleNamespace(data_dir=env.store.settings.data_dir),
        vault=SimpleNamespace(read=lambda _: SecretValue("worker-recovery-token")),
    )
    previous_workspace = {"value": None}
    attempts = []
    phases = []
    failures = []

    class RecoveryCoordinator:
        def __init__(self, _services, _runtime):
            self.services = _services
            self.coordinator = ScanCoordinator(_services, _runtime)
            self.coordinator.objects = SimpleNamespace(
                fetch=lambda *args, **kwargs: env.fixture["commit"],
                entries=lambda *args: [],
                repository=lambda *_: "fixture",
                run=lambda *args: b"c" * 40,
            )
            self.coordinator.scans = env.scans
            self.coordinator.backend = backend
            self.coordinator.workspaces = workspaces
            self.coordinator.runner = SimpleNamespace(
                check=lambda *args, **kwargs: {"exit_code": 0}
            )

        def run(self, token):
            attempts.append(token.run_id)
            phases.append("start")
            executor = BudgetedExecutor(env.budgets, env.queue, clock=services.clock)
            if previous_workspace["value"] is None:
                phases.append("initial_create")
                previous_workspace["value"] = self.coordinator.workspaces.create(
                    token, env.fixture["commit"], image="fixture", now=services.clock()
                )["id"]
            with env.store.read() as records:
                workspace_snapshot = json.loads(
                    records.one("workspaces", id=previous_workspace["value"])["snapshot_json"]
                )
            environment["ready"] = len(attempts) == 3
            phases.append("before_rebuild")
            try:
                result = self.coordinator.rebuild(
                    token,
                    workspace_snapshot,
                    "python:pytest",
                    executor,
                )
            except Exception as error:
                failures.append(repr(error))
                raise
            phases.append("rebuilt" if result["ready"] else "rebuild_failed")
            if not result["ready"]:
                raise PermissionError("连续恢复预检失败，等待人工处理")
            try:
                batch_id = self.coordinator.scans.begin(
                    token,
                    env.fixture["commit"],
                    {"image": result["image"]},
                    now=services.clock(),
                )
                self.coordinator.scans.baseline(
                    token,
                    batch_id,
                    "repository:no-declared-checks",
                    None,
                    "not_applicable",
                    now=services.clock(),
                )
                self.coordinator.scans.validate_complete(token, batch_id, now=services.clock())
                self.coordinator.workspaces.remove(token, result["id"], now=services.clock())
                self.services.queue.complete_scan(
                    token,
                    now=services.clock(),
                    validated_sha=env.fixture["commit"],
                    batch_id=batch_id,
                )
                return {"state": "ended", "reason": "scan_complete"}
            except Exception as error:
                failures.append(repr(error))
                raise

    monkeypatch.setattr("repowatch.worker.scans.ScanCoordinator", RecoveryCoordinator)
    worker = Worker(services)

    for attempt in range(2):
        clock["now"] += 1
        result = worker.run_once()
        assert result["state"] == "blocked"
        with env.store.read() as records:
            reason = dict(records.find("block_reasons", task_id=env.task, resolved=0)[0])
        assert reason["code"] in {"scan_safety_boundary", "runtime_execution_rejected"}, phases
        resolved = ControlService(env.store).resolve_reason(
            "worker-retry-plan-" + str(attempt),
            reason["id"],
            expected_revision=reason["revision"],
            explanation="补齐离线依赖镜像后重新执行扫描基线",
            retry_plan={
                "steps": ["重建隔离工作区", "重新执行声明检查并完成扫描"],
                "success_condition": "基线和全部扫描分块有效并完成依赖交接",
            },
            retry_limits={"max_attempts": 1, "max_steps": 4, "max_active_ms": 120000},
            now=clock["now"],
        )
        assert resolved["retry_authorization_id"]
        with env.store.read() as records:
            revision = records.one("global_control", singleton=1)["revision"]
        clock["now"] += 1
        ControlService(env.store).resume(
            "worker-explicit-resume-" + str(attempt),
            expected_revision=revision,
            now=clock["now"],
        )

    clock["now"] += 1
    final_result = worker.run_once()
    assert final_result == {"state": "ended", "reason": "scan_complete"}, (
        final_result,
        phases,
        failures,
    )
    assert len(attempts) == 3
    with env.store.read() as records:
        scan = records.one("tasks", id=env.task)
        waiting = records.one("tasks", id=waiter)
        assert scan["state"] == "ended" and scan["outcome"] == "scan_complete"
        assert waiting["state"] == "queued" and waiting["wait_reason"] is None
        assert not records.find("scan_dependencies", waiting_task_id=waiter)
        assert records.one("workspaces", id=previous_workspace["value"])["state"] == "removed"
        assert len(records.find("business_events", kind="workspace_preflight")) == 4
        assert not records.find("budget_progress", owner_task_id=env.task)
    assert env.budgets.view(env.task)["used"]["recovery_rounds"] == 3
