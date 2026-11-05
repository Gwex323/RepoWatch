import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from repowatch.adapters.docker_workspaces import DockerWorkspaces, source_path
from repowatch.adapters.git_objects import GitObjects
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import ArtifactService
from repowatch.application.budgets import BudgetService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.task_records import TaskRecords
from repowatch.application.workspaces import WorkspaceService
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.scans import baseline_outcome
from repowatch.operations.migrate import migrate
from repowatch.runtime.git_handoff import create_fixture, git, git_environment
from repowatch.worker.runner import Runner


@pytest.mark.parametrize(
    "path", ["../x", "/etc/passwd", "x/../y", ".git/config", "a\\b", "C:x", "a//b"]
)
def test_workspace_paths_reject_escape(path):
    with pytest.raises(PermissionError):
        source_path(path)


def test_external_contents_are_reported_separately(tmp_path):
    fixture = create_fixture(tmp_path)
    objects = GitObjects(tmp_path / "objects")
    sha = objects.import_bundle("repo", fixture["bundle"])
    backend = DockerWorkspaces(tmp_path / "workspaces", objects, executable="docker")
    result = backend.create("repo", sha, task_id="task", mode="scan")
    assert not result["ready"]
    assert {gap["reason"] for gap in result["gaps"]} == {
        "lfs_entity_missing",
        "submodule_content_missing",
    }
    assert len(result["manifest"]) == len(fixture["entries"])


@pytest.mark.skipif(os.environ.get("REPOWATCH_DOCKER_TESTS") != "1", reason="显式 Docker 验收")
def test_real_docker_snapshot_runner_isolation_and_persistence(tmp_path, monkeypatch):
    fixture = create_fixture(tmp_path)
    environment = git_environment(tmp_path)
    for path in ("large.bin", "vendor/lib"):
        git(fixture["repository"], "update-index", "--force-remove", path, env=environment)
    tree = git(fixture["repository"], "write-tree", env=environment).stdout.decode().strip()
    sha = (
        git(
            fixture["repository"],
            "commit-tree",
            tree,
            "-m",
            "without external contents",
            env=environment,
        )
        .stdout.decode()
        .strip()
    )
    git(fixture["repository"], "update-ref", "refs/heads/main", sha, env=environment)
    bundle = tmp_path / "clean.bundle"
    git(fixture["repository"], "bundle", "create", str(bundle), "main", env=environment)
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repo = tasks.register_repository("workspace-test", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, "scan:test", mode="scan", now=time.time())
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=240000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="夹具",
        now=time.time(),
    )
    queue.set_pause(False, now=time.time())
    token = queue.claim(now=time.time(), ttl=300)
    objects = GitObjects(tmp_path / "objects")
    objects.import_bundle(repo, bundle)
    backend = DockerWorkspaces(tmp_path / "workspaces", objects)
    workspaces = WorkspaceService(store, backend)
    workspace = workspaces.create(token, sha, now=time.time())
    artifacts = ArtifactService(store, settings.artifacts_dir)
    services = SimpleNamespace(store=store, budgets=budgets, queue=queue, artifacts=artifacts)
    runner = Runner(services, workspaces)
    try:
        assert workspace["ready"]
        assert backend.read_source(workspace, task, "crlf.txt") == b"line1\r\nline2\r\n"
        with pytest.raises(PermissionError):
            backend.read_source(workspace, "another-task", "crlf.txt")
        with pytest.raises(PermissionError):
            backend.read_source(workspace, task, "link.txt")
        result = runner.check(
            token,
            workspace["id"],
            [
                "sh",
                "-c",
                "test -x run.sh && test -L link.txt && "
                "test -f Dir/File.txt && test -f Dir/file.txt && "
                "test ! -e /var/run/docker.sock && test ! -e /source/.git && "
                "! touch /source/unauthorized && printf cache > generated && "
                'test -z "$REPOWATCH_GIT_AUTH" && printf verified',
            ],
            timeout=15,
            operation_key="real-check",
        )
        assert result["exit_code"] == 0
        assert not result["timed_out"] and not result["log_truncated"]
        proof = artifacts.require(result["log_artifact_id"], run_id=token.run_id, now=time.time())
        assert b"verified" in (artifacts.root / proof["relative_path"]).read_bytes()
        second = runner.check(
            token,
            workspace["id"],
            ["sh", "-c", "test ! -e generated"],
            timeout=10,
            operation_key="independent-copy",
        )
        assert second["exit_code"] == 0
        assert (
            runner.check(
                token,
                workspace["id"],
                ["sh", "-c", "test ! -e generated"],
                timeout=10,
                operation_key="independent-copy",
            )
            == second
        )
        with pytest.raises(PermissionError, match="参数已变化"):
            runner.check(
                token,
                workspace["id"],
                ["sh", "-c", "test ! -e generated"],
                timeout=11,
                operation_key="independent-copy",
            )
        old_log = artifacts.require(second["log_artifact_id"], now=time.time())
        (artifacts.root / old_log["relative_path"]).unlink()
        recollected = runner.check(
            token,
            workspace["id"],
            ["sh", "-c", "test ! -e generated"],
            timeout=10,
            operation_key="independent-copy",
        )
        assert recollected["exit_code"] == 0
        assert recollected["log_artifact_id"] != second["log_artifact_id"]
        with store.read() as records:
            assert not records.one("global_control", singleton=1)["paused"]
        prepared = runner.check(
            token,
            workspace["id"],
            ["sh", "-c", "test -f /deps/fixture"],
            timeout=10,
            operation_key="dependency-preparation",
            preparation={
                "steps": [["sh", "-c", "printf ready > /deps/fixture"]],
                "gaps": [],
                "declarations": [],
            },
        )
        assert prepared["exit_code"] == 0
        unavailable = runner.check(
            token,
            workspace["id"],
            ["sh", "-c", "exit 0"],
            timeout=10,
            operation_key="private-dependency",
            preparation={
                "steps": [],
                "gaps": [
                    {
                        "path": "requirements.txt",
                        "reason": "private_dependency_requires_host_preparation",
                    }
                ],
                "declarations": [],
            },
        )
        assert unavailable["exit_code"] == 125
        assert unavailable["preparation_report"] == {
            "reason": "dependency_preparation_unavailable",
            "gaps": [
                {
                    "path": "requirements.txt",
                    "reason": "private_dependency_requires_host_preparation",
                }
            ],
            "steps": [],
        }
        assert baseline_outcome(unavailable, b"") == "unable"
        original_guard = queue.guard
        calls = []

        def interrupt(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) >= 3:
                raise PermissionError("control stop")
            return original_guard(*args, **kwargs)

        monkeypatch.setattr(queue, "guard", interrupt)
        stopped = runner.check(
            token,
            workspace["id"],
            ["sh", "-c", "sleep 15"],
            timeout=20,
            operation_key="controlled-stop",
        )
        monkeypatch.setattr(queue, "guard", original_guard)
        assert stopped["control_stopped"]
        assert baseline_outcome(stopped, b"stopped") == "unable"
        with store.read() as records:
            assert len(records.find("runner_results")) == 6
            assert all(row["state"] == "stopped" for row in records.find("command_states"))
        previous = workspace
        workspace = workspaces.rebuild(token, previous["id"], now=time.time())
        assert workspace["ready"] and workspace["sha"] == previous["sha"]
        assert workspace["image"] == previous["image"]
        assert workspace["manifest_sha256"] == previous["manifest_sha256"]
        assert not backend.exists(previous)
        with store.read() as records:
            assert records.one("workspaces", id=previous["id"])["state"] == "removed"
            assert records.find("business_events", kind="workspace_preflight")
        workspaces.remove(token, workspace["id"], now=time.time())
        with pytest.raises(PermissionError):
            workspaces.require(token, workspace["id"], now=time.time())
        queue.complete(token, now=time.time())
    finally:
        if backend.exists(workspace):
            backend.remove(workspace)
        store.close()


def test_loader_uses_fixed_image_and_archive_spools(tmp_path, monkeypatch):
    objects = SimpleNamespace(
        entries=lambda *args: [
            {
                "path": "big.txt",
                "oid": "a" * 40,
                "mode": "100644",
                "kind": "blob",
            }
        ],
        blob=lambda *args: b"x" * (9 * 1024 * 1024),
    )
    backend = DockerWorkspaces(tmp_path, objects, executable="docker")
    archive, _, _ = backend.archive("repo", "a" * 40)
    try:
        assert archive._rolled
    finally:
        archive.close()
    commands = []
    digest = "sha256:" + "a" * 64

    def docker(*args, **kwargs):
        commands.append(args)
        output = digest.encode() if args[:2] == ("image", "inspect") else b""
        if args[0] == "inspect":
            output = b'[{"State":{"ExitCode":0}}]'
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(backend, "docker", docker)
    result = backend.create("repo", "a" * 40, task_id="task", mode="scan", image="configured")
    loader = next(command for command in commands if command[0] == "create")
    assert digest in loader and "alpine/git:latest" not in loader
    assert result["image"] == digest
