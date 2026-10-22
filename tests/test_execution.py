import io
import json
import tarfile
import time
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from test_github_accounts import TOKEN_A, MemoryVault, Remote

from repowatch.app import create_app
from repowatch.docker_runtime import DockerRuntime
from repowatch.execution import Execution
from repowatch.github_accounts import GitHubAccounts
from repowatch.repository_source import (
    ExecutionError,
    RepositorySource,
    discover,
    unpack_archive,
)
from repowatch.store import Conflict, Store

SHA = "a" * 40


def archive(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, value in files.items():
            info = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
            else:
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
    return data.getvalue()


class Source:
    def download(self, repo, account_id, version, destination, check):
        check()
        unpack_archive(archive({"root/requirements.txt": b"", "root/app.py": b"print('ok')"}),
                       destination)
        return SHA, {**discover(destination), "test": "python -c 'print(42)'"}


class Runtime:
    def __init__(self):
        self.fail = False
        self.running = True
        self.removed = []
        self.code = 0
        self.calls = []

    def readiness(self):
        return {"ready": not self.fail, "reason": "docker_unavailable" if self.fail else ""}

    def image(self, language, check):
        check()
        return "sha256:" + "a" * 64

    def seed(self, env, source, check):
        assert (source / "app.py").is_file()
        check()

    def run(self, env, action, check):
        check()
        self.calls.append(action)
        return 49152 if action == "start" else (self.code, "fixture output")

    def remove(self, env):
        if self.fail:
            raise ExecutionError("docker_unavailable")
        self.removed.append(env["id"])

    def cleanup(self, env):
        self.remove(env)

    def inspect(self, env):
        return {"Running": self.running, "ExitCode": 0}

    def logs(self, env):
        return "fixture output"

    def listening(self, env):
        return True


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "test.db")
    github = GitHubAccounts(store, Remote().client, MemoryVault())
    github.connect("alice", TOKEN_A)
    repo = store.add_repository("alice/project", "https://github.com/alice/project")
    runtime = Runtime()
    return Execution(store, github, runtime=runtime, source=Source()), repo, runtime


def prepare(service):
    execution, repo, _ = service
    env = execution.create(repo["id"])
    assert execution.tick()
    env = execution.get(env["id"])
    assert env["state"] == "downloaded"
    execution.action(env["id"], env["version"], "setup")
    execution.tick()
    return execution.get(env["id"])


@pytest.mark.parametrize("name,value", [
    ("root/../escape", b"x"), ("/root/file", b"x"), ("root/C:/file", b"x"),
    ("root/a\\b", b"x"), ("root/.git/config", b"x"), ("root/con.txt", b"x"),
    ("root/file", "../../outside"), ("root/foo. ", b"x"), ("root/a/./b", b"x"),
])
def test_archive_rejects_unsafe_entries_before_writing(tmp_path, name, value):
    destination = tmp_path / "source"
    with pytest.raises(ExecutionError):
        unpack_archive(archive({name: value}), destination)
    assert not destination.exists()
    assert not (tmp_path / "escape").exists()


def test_archive_validates_limits_case_collisions_and_multiple_roots(tmp_path, monkeypatch):
    for files in ({"a/A": b"a", "a/a": b"b"}, {"a/one": b"a", "b/two": b"b"}):
        with pytest.raises(ExecutionError):
            unpack_archive(archive(files), tmp_path / "source")
    monkeypatch.setattr("repowatch.repository_source.MAX_SOURCE", 1)
    with pytest.raises(ExecutionError, match="archive_limit"):
        unpack_archive(archive({"root/big": b"ab"}), tmp_path / "source")


def test_discovery_is_data_only_and_supports_language_overrides(tmp_path):
    unpack_archive(archive({"root/package.json": json.dumps({
        "scripts": {"start": "node app.js", "test": "touch /host/pwned"},
    }).encode()}), tmp_path / "node")
    config = discover(tmp_path / "node")
    assert config["language"] == "typescript" and config["test"] == "npm test"
    assert not config["install_network"]
    unpack_archive(archive({"root/go.mod": b"module fixture\n\ngo 1.25\n"}),
                   tmp_path / "go")
    assert discover(tmp_path / "go")["test"] == "go test ./..."
    unpack_archive(archive({"root/go.mod": b"module fixture",
                            "root/requirements.txt": b""}), tmp_path / "mixed")
    assert not discover(tmp_path / "mixed")["detected"]


@pytest.mark.parametrize("pytest_config", [
    {"pytest.ini": b"[pytest]\ntestpaths = tests\n"},
    {"requirements-dev.txt": b"-r requirements.txt\npytest>=8\npytest-asyncio>=0.23\n"},
    {"pyproject.toml": b"[tool.pytest.ini_options]\ntestpaths = ['tests']\n"},
    {"pyproject.toml": b"[project.optional-dependencies]\ntest = ['pytest>=8']\n"},
])
def test_python_discovery_selects_declared_pytest_and_installs_dev_dependencies(
    tmp_path, pytest_config,
):
    files = {
        "requirements.txt": b"fastapi>=0.115\n",
        "tests/test_sample.py": b"def test_sample():\n    assert True\n",
        **pytest_config,
    }
    unpack_archive(archive({"root/" + name: value for name, value in files.items()}),
                   tmp_path / "source")
    config = discover(tmp_path / "source")
    assert config["detected"]
    assert config["test"] == "/workspace/home/venv/bin/python -m pytest"
    assert ("pip install -r requirements-dev.txt" in config["install"]) == (
        "requirements-dev.txt" in pytest_config
    )
    assert not config["install_network"]


def test_python_discovery_keeps_unittest_without_pytest_evidence(tmp_path):
    unpack_archive(archive({
        "root/requirements.txt": b"# pytest is not used\n",
        "root/tests/test_sample.py": b"import unittest\n",
    }), tmp_path / "source")
    config = discover(tmp_path / "source")
    assert config["test"].endswith("-m unittest discover -s tests")
    assert "requirements-dev" not in config["install"]


def test_python_discovery_respects_pytest_configuration_without_tests_directory(tmp_path):
    unpack_archive(archive({
        "root/requirements.txt": b"pytest>=8\n",
        "root/pytest.ini": b"[pytest]\ntestpaths = checks\n",
        "root/checks/test_sample.py": b"def test_sample():\n    assert True\n",
    }), tmp_path / "source")
    assert discover(tmp_path / "source")["test"].endswith("-m pytest")


def test_authenticated_download_uses_fixed_revision_and_drops_auth_on_redirect(service, tmp_path):
    execution, repo, _ = service
    original = execution.github.client.request
    calls = []

    def metadata(path, token):
        if path.endswith("/commits/HEAD"):
            return {"sha": SHA}, {}
        return original(path, token)

    def handle(request):
        calls.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer " + TOKEN_A
            assert request.url.path.endswith("/tarball/" + SHA)
            return httpx.Response(302, headers={
                "Location": f"https://codeload.github.com/alice/project/legacy.tar.gz/{SHA}",
            })
        assert "authorization" not in request.headers
        return httpx.Response(200, content=archive({"root/go.mod": b"module fixture"}))

    with patch.object(execution.github.client, "request", side_effect=metadata):
        source = RepositorySource(execution.github, httpx.MockTransport(handle))
        revision, _ = source.download(repo, 101, 1, tmp_path / "source", lambda: None)
    assert revision == SHA and len(calls) == 2
    assert TOKEN_A.encode() not in (tmp_path / "source/go.mod").read_bytes()


def test_download_rejects_external_redirect(service, tmp_path):
    execution, repo, _ = service
    execution.github.client.repository = lambda *_: {"repository": repo["name"]}
    execution.github.validated_identity = lambda *_: (TOKEN_A, {"id": 101})
    with patch.object(execution.github.client, "request", return_value=({"sha": SHA}, {})):
        source = RepositorySource(execution.github, httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://attacker.invalid/"})
        ))
        with pytest.raises(ExecutionError, match="download_redirect_rejected"):
            source.download(repo, 101, 1, tmp_path / "source", lambda: None)


def test_durable_lifecycle_and_origin_gates_remain_independent(service):
    execution, repo, runtime = service
    first = execution.create(repo["id"])
    assert execution.create(repo["id"])["id"] == first["id"]
    execution.tick()
    env = execution.get(first["id"])
    with pytest.raises(Conflict, match="setup_required"):
        execution.action(env["id"], env["version"], "test")
    execution.action(env["id"], env["version"], "setup")
    execution.tick()
    env = execution.get(env["id"])
    assert env["installed"] and env["state"] == "ready"
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    env = execution.get(env["id"])
    assert env["test_result"]["revision"] == SHA and env["test_result"]["exit_code"] == 0
    assert execution.store.dashboard()["findings"] == []
    execution.action(env["id"], env["version"], "start")
    with patch("repowatch.execution.socket.create_connection"):
        execution.tick()
    env = execution.get(env["id"])
    assert env["url"] == "http://127.0.0.2:49152"
    with pytest.raises(Conflict, match="environment_not_configurable"):
        execution.configure(env["id"], env["version"], env["config"])
    execution.action(env["id"], env["version"], "stop")
    execution.tick()
    restarted = Execution(Store(execution.store.path), execution.github,
                          runtime=runtime, source=Source())
    env = restarted.get(env["id"])
    assert env["state"] == "stopped" and env["installed"]
    restarted.action(env["id"], env["version"], "start")
    with patch("repowatch.execution.socket.create_connection"):
        restarted.tick()
    assert restarted.get(env["id"])["state"] == "running"
    restarted.update(env["id"], expires=time.time() - 1)
    restarted.reconcile()
    assert restarted.get(env["id"])["state"] == "stopped"


def test_failures_configuration_and_cancellation(service):
    execution, _, runtime = service
    env = prepare(service)
    runtime.code = 7
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    env = execution.get(env["id"])
    assert env["state"] == "test_failed" and env["test_result"]["exit_code"] == 7
    assert env["jobs"][0]["log"] == "fixture output"
    with pytest.raises(Conflict, match="environment_changed"):
        execution.configure(env["id"], 1, env["config"])
    config = {**env["config"], "test": "echo changed"}
    env = execution.configure(env["id"], env["version"], config)
    assert env["test_result"] is None and env["installed"]
    execution.action(env["id"], env["version"], "setup")
    env = execution.get(env["id"])
    with pytest.raises(Conflict, match="environment_busy"):
        execution.configure(env["id"], env["version"], config)
    execution.action(env["id"], env["version"], "stop")
    execution.tick()
    assert execution.get(env["id"])["state"] == "stopped"


def test_interrupted_job_is_not_replayed_and_cleanup_retries(service):
    execution, _, runtime = service
    env = prepare(service)
    execution.action(env["id"], env["version"], "test")
    job = execution.claim()
    with execution.store.connection() as db:
        db.execute("UPDATE execution_jobs SET lease=0 WHERE id=?", (job["id"],))
    runtime.fail = True
    execution.reconcile()
    assert execution.get(env["id"])["busy"]
    with execution.store.connection() as db:
        db.execute("UPDATE execution_jobs SET lease=0 WHERE id=?", (job["id"],))
    runtime.fail = False
    execution.reconcile()
    env = execution.get(env["id"])
    assert env["state"] == "stopped" and not env["busy"]
    assert runtime.calls == ["setup"]
    execution.action(env["id"], env["version"], "delete")
    execution.tick()
    assert execution.get(env["id"])["state"] == "deleted"
    assert not execution.directory(env["id"]).exists()


def test_container_policy_has_no_secrets_host_mounts_or_privileges():
    runtime = DockerRuntime("test")
    env = {"id": "env_" + "a" * 16, "image_id": "sha256:" + "b" * 64,
           "config": {"port": 8000}}
    args = runtime.create_args(env, "start", "python app.py", network="internal-fixture")
    command = " ".join(args)
    assert "--user 1000:1000" in command and "--read-only" in args
    assert "--cap-drop ALL" in command and "no-new-privileges:true" in args
    assert "--memory 1g" in command and "--pids-limit 256" in command
    assert "type=volume," in command and "type=bind" not in command
    assert "--publish" not in args and "1800s" in args
    assert TOKEN_A not in command and "docker.sock" not in command
    assert "--privileged" not in args and "--network host" not in command


def test_preview_is_separate_without_project_mounts():
    runtime = DockerRuntime("test")
    env = {"id": "env_" + "a" * 16, "config": {"port": 8000}, "state": "ready"}
    name, _, network = runtime.names(env)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if "inspect" in args and ".NetworkSettings.Networks" in " ".join(args):
            return 0, json.dumps({network: {"IPAddress": "172.30.0.2"}})
        if "inspect" in args and ".NetworkSettings.Ports" in " ".join(args):
            return 0, json.dumps({"8000/tcp": [{"HostIp": "127.0.0.2", "HostPort": "49152"}]})
        return 0, ""

    runtime.command = command
    runtime.image = lambda *_: "sha256:" + "a" * 64
    assert runtime.preview(env, lambda: None) == 49152
    create = next(call for call in calls if call[0] == "create")
    assert "--mount" not in create and "--env" not in create
    assert "net.ipv4.ip_forward=0" in create and "127.0.0.2::8000" in create
    assert ["network", "connect", network, name + "-preview"] in calls


def test_running_cancel_and_shutdown_keep_environment_recoverable(service):
    execution, _, runtime = service
    env = prepare(service)
    original = runtime.run

    def cancel_during_run(env, action, check):
        current = execution.get(env["id"])
        execution.action(env["id"], current["version"], "stop")
        check()
        raise AssertionError("Cancelled operation continued")

    runtime.run = cancel_during_run
    execution.action(env["id"], env["version"], "test")
    execution.tick()
    env = execution.get(env["id"])
    assert env["error"] == "execution_cancelled" and not env["busy"]
    runtime.run = original
    execution.action(env["id"], env["version"], "start")
    with patch("repowatch.execution.socket.create_connection"):
        execution.tick()
    execution.stop_previews()
    env = execution.get(env["id"])
    assert env["state"] == "stopped" and env["url"] is None and env["installed"]


def test_engine_outage_is_visible_and_cleanup_recovers(service):
    execution, _, runtime = service
    env = prepare(service)
    execution.action(env["id"], env["version"], "start")
    with patch("repowatch.execution.socket.create_connection"):
        execution.tick()
    env = execution.get(env["id"])
    runtime.fail = True
    execution.action(env["id"], env["version"], "stop")
    execution.tick()
    env = execution.get(env["id"])
    assert env["error"] == "cleanup_pending" and env["url"] is None
    assert not env["busy"]
    runtime.fail = False
    execution.reconcile()
    assert execution.get(env["id"])["state"] == "stopped"


def test_execution_api_requires_owner_csrf_and_strict_bodies(tmp_path):
    remote, vault = Remote(), MemoryVault()
    app = create_app(tmp_path / "api.db", start_worker=False, github_client=remote.client,
                     credential_vault=vault, execution_runtime=Runtime(),
                     repository_source=Source())
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/execution/readiness").status_code == 401
        auth = client.post("/api/setup", json={"password": "test-password-123"}).json()
        headers = {"x-csrf-token": auth["csrf"]}
        assert client.post("/api/repositories/bad/environments", json={}).status_code == 403
        client.post("/api/github/accounts", headers=headers,
                    json={"username": "alice", "token": TOKEN_A})
        repo = client.post("/api/repositories", headers=headers,
                           json={"url": "https://github.com/alice/project"}).json()
        path = f"/api/repositories/{repo['id']}/environments"
        assert client.post(path, json={"account_id": 222}, headers=headers).status_code == 422
        created = client.post(path, json={}, headers=headers)
        assert created.status_code == 202
        assert TOKEN_A not in created.text
        assert client.get(path).json()[0]["account_id"] == 101
        app.state.execution.tick()
        env = client.get(path).json()[0]
        malicious = {**env["config"], "version": env["version"], "image": "attacker/image"}
        assert client.post(f"/api/environments/{env['id']}/configuration",
                           json=malicious, headers=headers).status_code == 422
