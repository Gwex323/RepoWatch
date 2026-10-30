"""Mock-only environment recovery and immutable dependency-cache regression tests."""

import asyncio
import copy
import json

import pytest
from test_execution import Runtime, archive, service  # noqa: F401
from test_repairs import confirm, enable, pending  # noqa: F401

from repowatch.environment_reuse import dependency_plan
from repowatch.repository_source import ExecutionError, discover, unpack_archive
from repowatch.store import Conflict


class VersionedSource:
    revision = "a" * 40
    content = b"print('first')"
    requirements = b""

    def download(self, repo, account_id, version, destination, check):
        check()
        unpack_archive(archive({"root/requirements.txt": self.requirements,
                                "root/app.py": self.content}), destination)
        return self.revision, discover(destination)


class SnapshotRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.workspaces, self.snapshots = {}, {}
        self.seed_calls, self.snapshot_calls, self.restore_calls = [], [], []
        self.image_id = "sha256:" + "a" * 64
        self.restore_error = None
        self.capture_error = False

    def image(self, language, check):
        check()
        return self.image_id

    def seed(self, env, source, check):
        check()
        self.seed_calls.append(env["id"])
        self.workspaces[env["id"]] = {
            "project": {p.relative_to(source).as_posix(): p.read_bytes()
                        for p in source.rglob("*") if p.is_file()}, "home": {},
        }

    def run(self, env, action, check):
        result = super().run(env, action, check)
        if action == "setup":
            self.workspaces[env["id"]]["home"]["dependency"] = b"clean"
        return result

    def snapshot_dependencies(self, env, cache_id, check):
        check()
        self.snapshot_calls.append(cache_id)
        if self.capture_error:
            raise ExecutionError("docker_command_failed")
        self.snapshots[cache_id] = copy.deepcopy(self.workspaces[env["id"]]["home"])

    def restore_dependencies(self, env, source, cache_id, check):
        self.restore_calls.append(cache_id)
        if self.restore_error:
            raise ExecutionError(self.restore_error)
        check()
        self.seed(env, source, check)
        self.workspaces[env["id"]]["home"] = copy.deepcopy(self.snapshots[cache_id])

    def delete_dependencies(self, env, cache_id):
        self.snapshots.pop(cache_id, None)


@pytest.fixture
def optimized(service):  # noqa: F811
    execution, repo, _ = service
    runtime, source = SnapshotRuntime(), VersionedSource()
    execution.runtime, execution.source = runtime, source
    return execution, repo, runtime, source


def download(optimized):
    execution, repo, _, _ = optimized
    env = execution.create(repo["id"])
    execution.tick()
    return execution.get(env["id"])


def setup(optimized, env):
    execution, _, _, _ = optimized
    execution.action(env["id"], env["version"], "setup")
    execution.tick()
    return execution.get(env["id"])


def due(repairs, task_id):
    with repairs.store.connection() as db:
        context = json.loads(db.execute("SELECT context FROM repair_tasks WHERE id=?",
                                        (task_id,)).fetchone()[0])
        context["environment_wait"]["next_check"] = 0
        db.execute("UPDATE repair_tasks SET context=? WHERE id=?", (json.dumps(context), task_id))


def test_docker_wait_is_persistent_and_resumes_without_spending_attempts(pending):  # noqa: F811
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    runtime.fail = True
    assert not asyncio.run(repairs.tick())
    waiting = repairs.get(task["id"])
    assert waiting["status"] == "waiting_environment" and waiting["attempt"] == 0
    assert not repairs.engine.models and not runtime.test_calls and not publisher.calls
    assert not asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["version"] == waiting["version"]
    runtime.fail = False
    due(repairs, task["id"])
    assert asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created" and result["attempt"] == 1
    assert len(repairs.engine.models) == len(publisher.calls) == 1
    assert not result["context"].get("environment_recovery_used")
    assert "environment_wait" not in result["context"]


@pytest.mark.parametrize("gate", ["paused", "model_disabled", "stale", "source", "account"])
def test_environment_recovery_rechecks_all_authorization_gates(pending, gate):  # noqa: F811
    repairs, _, _, runtime, publisher = pending
    task = confirm(pending)
    enable(repairs)
    runtime.fail = True
    asyncio.run(repairs.tick())
    due(repairs, task["id"])
    runtime.fail = False
    if gate == "paused":
        settings = repairs.settings()
        repairs.configure(settings["version"], False)
    elif gate == "model_disabled":
        repairs.engine.enabled = False
    elif gate == "source":
        (repairs.execution.directory(task["env_id"]) / "source/app.py").write_text("changed")
    else:
        with repairs.store.connection() as db:
            if gate == "stale":
                db.execute("UPDATE analysis_findings SET status='unconfirmed'")
            else:
                db.execute("UPDATE github_configuration SET active_account_id=NULL")
    assert not asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["attempt"] == 0 and not repairs.engine.models and not publisher.calls
    assert result["status"] == ("waiting_environment" if gate in ("paused", "model_disabled")
                                else "failed")


def historical_failure(pending):  # noqa: F811
    repairs, _, _, runtime, _ = pending
    task = confirm(pending)
    enable(repairs)
    with repairs.store.connection() as db:
        db.execute("UPDATE repair_tasks SET status='failed',stage='repair_agent',"
                   "error='docker_unavailable',attempt=3 WHERE id=?", (task["id"],))
        for kind, value in [
            ("retry", "3"), ("workspace_recovery", "old_policy"),
            ("failure_details", json.dumps({"code": "docker_unavailable",
                "exceptions": ["ExecutionError"],
                "locations": [{"file": "repowatch/repairs.py", "line": 602}]})),
            ("failure", "docker_unavailable"),
        ]:
            db.execute("INSERT INTO repair_events(repair_id,kind,value,created) VALUES(?,?,?,1)",
                       (task["id"], kind, value))
    return repairs, repairs.get(task["id"]), runtime


def test_old_exhausted_preflight_requires_explicit_retry_and_credits_only_once(pending):  # noqa: F811
    repairs, task, runtime = historical_failure(pending)
    assert task["docker_recovery_available"] and task["can_retry"]
    assert not asyncio.run(repairs.tick())
    assert not repairs.engine.models
    runtime.regression_after = 1
    queued = repairs.retry(task["id"], task["version"])
    assert queued["attempts"][0]["attempt"] == 3
    assert queued["context"]["environment_recovery_used"] is True
    asyncio.run(repairs.tick())
    failed = repairs.get(task["id"])
    assert failed["attempt"] == 4 and failed["error"] == "repair_tests_failed"
    assert failed["can_retry"] and not failed["docker_recovery_available"]
    repairs.retry(task["id"], failed["version"])


@pytest.mark.parametrize("evidence", ["request_started", "agent", "testing", "tool_call"])
def test_old_recovery_cannot_refund_an_attempt_that_entered_model_work(pending, evidence):  # noqa: F811
    repairs, task, _ = historical_failure(pending)
    repairs.event(task["id"], evidence, "synthetic")
    current = repairs.get(task["id"])
    assert not current["docker_recovery_available"] and current["can_retry"]


def test_new_revision_clones_clean_snapshot_and_does_not_inherit_test_evidence(optimized):
    execution, _, runtime, source = optimized
    first = setup(optimized, download(optimized))
    assert first["installed"] and first["preparation"] == "full_install"
    runtime.workspaces[first["id"]]["home"]["dependency"] = b"runtime-mutated"
    source.revision, source.content = "b" * 40, b"print('second')"
    second = setup(optimized, download(optimized))
    assert second["id"] != first["id"] and second["preparation"] == "dependencies_reused"
    assert runtime.calls == ["setup"] and len(runtime.restore_calls) == 1
    assert runtime.workspaces[second["id"]]["home"]["dependency"] == b"clean"
    assert runtime.workspaces[second["id"]]["project"]["app.py"] == source.content
    assert runtime.workspaces[first["id"]]["project"]["app.py"] == b"print('first')"
    assert second["test_result"] is None and second["jobs"][0]["action"] == "setup"
    assert not second["jobs"][0]["error"]
    assert second["revision"] == source.revision
    assert second["test_result"] is None


def test_identical_revision_reuses_installed_environment_and_keeps_download_audit(optimized):
    execution, _, runtime, _ = optimized
    first = setup(optimized, download(optimized))
    downloaded = download(optimized)
    assert downloaded["state"] == "reused"
    assert downloaded["reused_environment_id"] == first["id"]
    assert runtime.calls == ["setup"]
    with pytest.raises(Conflict, match="environment_reused"):
        execution.action(downloaded["id"], downloaded["version"], "setup")


@pytest.mark.parametrize("change", ["dependencies", "image", "command", "source_mismatch"])
def test_incompatible_inputs_do_not_silently_reuse_environment_or_dependencies(optimized, change):
    execution, _, runtime, source = optimized
    first = setup(optimized, download(optimized))
    if change == "source_mismatch":
        source.content = b"changed bytes with same revision"
        assert download(optimized)["reused_environment_id"] is None
        return
    source.revision = "b" * 40
    if change == "dependencies":
        source.requirements = b"demo==1.0\n"
    if change == "image":
        runtime.image_id = "sha256:" + "b" * 64
    second = download(optimized)
    if change == "command":
        config = {key: second["config"][key] for key in (
            "language", "install", "test", "start", "port", "install_network",
        )}
        config["install"] = "echo custom setup"
        second = execution.configure(second["id"], second["version"], config)
    result = setup(optimized, second)
    assert result["installed"] and result["preparation"] == "full_install"
    assert runtime.calls == ["setup", "setup"] and not runtime.restore_calls
    assert execution.get(first["id"])["installed"]


@pytest.mark.parametrize("failure", ["docker_command_failed", "execution_cancelled"])
def test_missing_cache_falls_back_but_cancellation_never_runs_install(optimized, failure):
    _, _, runtime, source = optimized
    setup(optimized, download(optimized))
    source.revision = "b" * 40
    runtime.restore_error = failure
    result = setup(optimized, download(optimized))
    assert result["installed"] == (failure != "execution_cancelled")
    assert len(runtime.calls) == (1 if failure == "execution_cancelled" else 2)


def test_cache_capture_failure_does_not_discard_successful_install(optimized):
    _, _, runtime, _ = optimized
    runtime.capture_error = True
    result = setup(optimized, download(optimized))
    assert result["installed"] and result["preparation"] == "full_install"


def test_snapshot_slots_are_bounded_per_repository_and_identity(optimized):
    execution, _, runtime, source = optimized
    for index in range(4):
        source.revision = str(index + 1) * 40
        source.requirements = f"demo=={index}.0\n".encode()
        assert setup(optimized, download(optimized))["installed"]
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM dependency_snapshots").fetchone()[0] == 3
    assert len(runtime.snapshot_calls) == 3


@pytest.mark.parametrize("files", [
    {"requirements.txt": b"-e .\n"},
    {"requirements.txt": b"-r requirements-dev.txt\n"},
    {"requirements.txt": b"demo>=1\n"},
    {"requirements.txt": b"demo==1", "setup.py": b"print('build hook')"},
    {"go.mod": b"module example\ngo 1.25\nreplace foo => ../foo\n"},
    {"package.json": b'{"scripts":{"prepare":"node build.js"}}',
     "package-lock.json": b'{"packages":{"":{}}}'},
])
def test_source_sensitive_install_inputs_fall_back_to_full_setup(tmp_path, files):
    root = tmp_path / "source"
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    config = discover(root)
    assert dependency_plan(root, config, "sha256:" + "a" * 64) is None


@pytest.mark.parametrize("language,files", [
    ("go", {"go.mod": b"module sample\n\ngo 1.25\n", "main.go": b"package main"}),
    ("typescript", {"package.json": b'{"name":"sample","version":"1.0.0"}',
      "package-lock.json": b'{"lockfileVersion":3,"packages":{"":{"version":"1.0.0"}}}'}),
])
def test_dependency_keys_bind_image_inputs_not_source_code(tmp_path, language, files):
    root = tmp_path / "source"
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    config = discover(root)
    original = dependency_plan(root, config, "sha256:" + "a" * 64)
    assert original and config["language"] == language
    (root / "unrelated_source.txt").write_text("new revision")
    assert dependency_plan(root, config, "sha256:" + "a" * 64) == original
    assert dependency_plan(root, config, "sha256:" + "b" * 64) != original


def test_python_stdlib_shadowing_and_custom_setup_disable_snapshot_reuse(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / "sitecustomize.py").write_text("print('source-sensitive install')")
    assert dependency_plan(root, discover(root), "sha256:" + "a" * 64) is None


def test_dependency_copy_uses_readonly_cache_and_cleans_failed_helper(monkeypatch):
    from repowatch.docker_runtime import DockerRuntime

    runtime = DockerRuntime("synthetic")
    calls = []
    monkeypatch.setattr(runtime, "_owned_volume", lambda env: "volume-" + env["id"])

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["start", "--attach"]:
            raise ExecutionError("execution_cancelled")
        return 0, ""

    monkeypatch.setattr(runtime, "command", command)
    origin = {"id": "cache_" + "a" * 16}
    target = {"id": "env_" + "b" * 16, "image_id": "sha256:" + "a" * 64}
    with pytest.raises(ExecutionError, match="execution_cancelled"):
        runtime._copy_dependencies(origin, target, lambda: None)
    created = calls[0]
    assert "type=volume,src=volume-" + origin["id"] + ",dst=/from,readonly" in created
    assert created[created.index("--network") + 1] == "none"
    assert created[created.index("--user") + 1] == "1000:1000"
    assert calls[-1][:2] == ["rm", "-f"]


def test_foreign_snapshot_volume_is_never_written_or_deleted(monkeypatch):
    from repowatch.docker_runtime import DockerRuntime

    runtime, calls = DockerRuntime("synthetic"), []

    def command(args, **kwargs):
        calls.append(args)
        assert args[:2] == ["volume", "inspect"]
        return 0, json.dumps([{"Labels": {"repowatch.instance": "someone-else"}}])

    monkeypatch.setattr(runtime, "command", command)
    env = {"id": "env_" + "b" * 16, "image_id": "sha256:" + "a" * 64}
    with pytest.raises(ExecutionError, match="docker_resource_mismatch"):
        runtime.delete_dependencies(env, "cache_" + "a" * 16)
    assert len(calls) == 1



def test_seed_refuses_foreign_volume_before_copying_or_creating_container(monkeypatch):
    from repowatch.docker_runtime import DockerRuntime

    runtime, calls = DockerRuntime("synthetic"), []
    monkeypatch.setattr(runtime, "remove", lambda env: None)

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["volume", "inspect"]:
            return 0, json.dumps([{"Labels": {"repowatch.instance": "foreign"}}])
        return 0, ""

    monkeypatch.setattr(runtime, "command", command)
    env = {"id": "cache_" + "a" * 16, "image_id": "sha256:" + "a" * 64}
    with pytest.raises(ExecutionError, match="docker_resource_mismatch"):
        runtime.seed(env, None, lambda: None)
    assert all(call[0] == "volume" for call in calls)


def test_git_dependency_cannot_claim_source_independent_npm_snapshot(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "package.json").write_text('{"name":"sample"}')
    (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/example": {"resolved": "git+ssh://example.invalid/package"},
    }}))
    assert dependency_plan(root, discover(root), "sha256:" + "a" * 64) is None



def test_source_change_between_preflight_and_copy_cannot_reach_model(pending):  # noqa: F811
    repairs, _, _, _, publisher = pending
    task = confirm(pending)
    enable(repairs)
    original = repairs._environment_candidate

    def race(**kwargs):
        candidate = original(**kwargs)
        (repairs.execution.directory(task["env_id"]) / "source/unanalysed.txt").write_text("race")
        return candidate

    repairs._environment_candidate = race
    assert asyncio.run(repairs.tick())
    assert repairs.get(task["id"])["error"] == "repair_source_changed"
    assert not repairs.engine.models and not publisher.calls
