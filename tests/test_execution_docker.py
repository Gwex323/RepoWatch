"""Opt-in real-engine validation using only authored, dependency-free fixtures."""

import os
import shutil
from pathlib import Path

import httpx
import pytest
from test_execution import SHA, service  # noqa: F401

from repowatch.docker_runtime import DockerRuntime
from repowatch.repository_source import discover

pytestmark = pytest.mark.skipif(
    os.getenv("REPOWATCH_DOCKER_TESTS") != "1", reason="Set REPOWATCH_DOCKER_TESTS=1 for Docker",
)


@pytest.mark.parametrize("language", ["python", "go", "typescript"])
def test_real_install_test_start_stop_restart_and_cleanup(service, language):  # noqa: F811
    execution, repo, _ = service
    execution.runtime = DockerRuntime(execution.store.setting("execution_namespace"))
    source_path = Path(__file__).parent / "fixtures/execution" / language

    class LocalSource:
        def download(self, repo, account_id, version, destination, check):
            check()
            shutil.copytree(source_path, destination)
            return SHA, discover(destination)

    execution.source = LocalSource()
    assert execution.runtime.readiness()["ready"]
    env = execution.create(repo["id"])

    def act(action):
        current = execution.get(env["id"])
        execution.action(env["id"], current["version"], action)
        execution.tick()
        return execution.get(env["id"])

    try:
        execution.tick()
        result = act("setup")
        assert result["installed"], (result["error"], result["jobs"][0]["log"])
        result = act("test")
        assert result["test_result"]["exit_code"] == 0, result["jobs"][0]["log"]
        for _ in range(2):
            result = act("start")
            assert result["state"] == "running", (result["error"], result["jobs"][0]["log"])
            with httpx.Client(trust_env=False) as client:
                response = client.get(result["url"], timeout=5)
            assert response.text == f"repowatch-{language}-fixture"
            assert act("stop")["state"] == "stopped"
        result = act("delete")
        assert result["state"] == "deleted"
    finally:
        execution.runtime.cleanup(execution.get(env["id"]))
