"""Opt-in real-engine validation using only authored, dependency-free fixtures."""

import os
import shutil
from pathlib import Path

import httpx
import pytest
from test_execution import SHA, service  # noqa: F401

from repowatch.docker_runtime import DockerRuntime
from repowatch.repair_workspace import Patch, RepairWorkspace
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


@pytest.mark.parametrize("language", ["python", "go", "typescript"])
def test_real_repair_fails_before_passes_after_and_preserves_original(service, language):  # noqa: F811
    execution, repo, _ = service
    execution.runtime = DockerRuntime(execution.store.setting("execution_namespace"))
    fixture = Path(__file__).parent / "fixtures/repair" / language
    files = {
        "python": (
            "bounds.py", "tests/test_regression.py", "python -m unittest discover -s tests",
            "python -m unittest discover -s tests -p test_regression.py",
            "import unittest\nfrom bounds import bound\n"
            "class Boundary(unittest.TestCase):\n"
            "    def test_limit(self):\n        self.assertEqual(bound(5, 5), 5)\n",
        ),
        "go": (
            "bounds.go", "regression_test.go", "go test ./...", "go test -run TestLimit .",
            'package bounds\nimport "testing"\nfunc TestLimit(t *testing.T) {\n'
            'if value := Bound(5, 5); value != 5 { t.Fatalf("got %d, want 5", value) }\n}\n',
        ),
        "typescript": (
            "bounds.ts", "regression.test.ts", "node --test *.test.ts",
            "node --test regression.test.ts",
            "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "import { bound } from './bounds.ts';\n"
            "test('inclusive limit', () => { assert.equal(bound(5, 5), 5); });\n",
        ),
    }
    source_name, regression_name, suite, targeted, test_code = files[language]
    env = execution.create(repo["id"])
    execution.tick()
    source = execution.directory(env["id"]) / "source"
    for file in fixture.rglob("*"):
        if file.is_file():
            destination = source / file.relative_to(fixture)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
    env = execution.get(env["id"])
    env["config"].update(language=language, install="true", test=suite, install_network=False)
    workspace = RepairWorkspace(execution, "repair_" + "c" * 16, env, lambda: None)
    original = workspace.read(source_name)
    replacement = original["text"].replace("maximum - 1", "maximum").replace("maximum-1", "maximum")
    workspace.prepare(Patch.model_validate({
        "summary": "Correct inclusive upper bound in authored fixture.",
        "edits": [
            {"path": source_name, "original_sha256": original["sha256"], "content": replacement},
            {"path": regression_name, "original_sha256": "", "content": test_code},
        ],
        "regression_paths": [regression_name], "regression_command": targeted,
    }))
    result = workspace.verify(lambda *args: None)
    assert result["passed"], result
    assert result["original_suite"]["exit_code"] == 0
    assert result["regression_before"]["exit_code"] != 0
    assert result["regression_after"]["exit_code"] == result["full_suite_after"]["exit_code"] == 0
    assert (source / source_name).read_text() == original["text"]
    assert not (source / regression_name).exists()


@pytest.mark.parametrize("language", ["python", "go", "typescript"])
def test_real_dependency_snapshot_reuse_isolated_from_runtime_mutation(service, language):  # noqa: F811
    import json

    execution, repo, _ = service
    runtime = DockerRuntime(execution.store.setting("execution_namespace"))
    execution.runtime = runtime
    fixture = Path(__file__).parent / "fixtures/execution" / language

    class LocalSource:
        revision = "a" * 40

        def download(self, repo, account_id, version, destination, check):
            check()
            shutil.copytree(fixture, destination)
            marker = {"python": "app.py", "go": "main.go", "typescript": "server.ts"}[language]
            with (destination / marker).open("a", encoding="utf-8") as stream:
                stream.write(("\n# " if language == "python" else "\n// ") + self.revision + "\n")
            if language == "typescript":
                package = json.loads((destination / "package.json").read_text())
                (destination / "package-lock.json").write_text(json.dumps({
                    "name": package["name"], "lockfileVersion": 3, "requires": True,
                    "packages": {"": {"name": package["name"]}},
                }))
            return self.revision, discover(destination)

    source = LocalSource()
    execution.source = source
    environments = []

    def downloaded_setup():
        env = execution.create(repo["id"])
        environments.append(env["id"])
        execution.tick()
        current = execution.get(env["id"])
        execution.action(env["id"], current["version"], "setup")
        execution.tick()
        result = execution.get(env["id"])
        assert result["installed"], (result["error"], result["jobs"][0]["log"])
        return result

    try:
        assert runtime.readiness()["ready"]
        first = downloaded_setup()
        mutation = {**first, "config": {**first["config"],
                                       "test": "touch /workspace/home/runtime_only"}}
        assert runtime.run(mutation, "test", lambda: None)[0] == 0
        source.revision = "b" * 40
        second = downloaded_setup()
        assert second["preparation"] == "dependencies_reused", second["jobs"][0]["log"]
        assert second["test_result"] is None
        validation = {**second, "config": {**second["config"], "test":
            "test ! -e /workspace/home/runtime_only && " + second["config"]["test"]}}
        result = runtime.run(validation, "test", lambda: None)
        assert result[0] == 0, result[1]
        same = execution.create(repo["id"])
        execution.tick()
        assert execution.get(same["id"])["reused_environment_id"] == second["id"]
    finally:
        with execution.store.connection() as db:
            caches = [row[0] for row in db.execute("SELECT id FROM dependency_snapshots")]
        if environments:
            template = execution.get(environments[0])
            for cache_id in caches:
                runtime.delete_dependencies(template, cache_id)
        for env_id in environments:
            runtime.cleanup(execution.get(env_id))



def test_real_automatic_pipeline_reuses_dependencies_and_verifies_http(service):  # noqa: F811
    import asyncio

    from test_repository_analysis import Engine, ReviewModel

    from repowatch.repository_analysis import Analyses, run_analysis

    execution, repo, _ = service
    runtime = DockerRuntime(execution.store.setting("execution_namespace"))
    execution.runtime = runtime
    fixture = Path(__file__).parent / "fixtures/execution/python"

    class LocalSource:
        revision = "a" * 40

        def download(self, repo, account_id, version, destination, check):
            check()
            shutil.copytree(fixture, destination)
            with (destination / "app.py").open("a") as stream:
                stream.write("\n# " + self.revision + "\n")
            return self.revision, discover(destination)

    class FixtureEngine(Engine):
        async def analyze(self, context, event):
            return await run_analysis(ReviewModel(findings=False), context, event)

    source = LocalSource()
    execution.source = source
    analyses = Analyses(execution, FixtureEngine())
    environments = []
    try:
        for revision in ("a" * 40, "b" * 40):
            source.revision = revision
            env = execution.create(repo["id"], auto_run=True)
            environments.append(env["id"])
            for _ in range(8):
                execution.tick()
                asyncio.run(analyses.tick())
                current = execution.get(env["id"])
                if current["pipeline"]["state"] in ("completed", "failed"):
                    break
            assert current["pipeline"]["state"] == "completed", current
            assert current["test_result"]["exit_code"] == 0
            assert current["access_result"]["status_code"] == 200
            if revision.startswith("b"):
                assert current["preparation"] == "dependencies_reused"
            with httpx.Client(trust_env=False) as client:
                assert client.get(current["url"]).text == "repowatch-python-fixture"
    finally:
        with execution.store.connection() as db:
            caches = [row[0] for row in db.execute("SELECT id FROM dependency_snapshots")]
        if environments:
            template = execution.get(environments[0])
            for cache_id in caches:
                runtime.delete_dependencies(template, cache_id)
        for env_id in environments:
            runtime.cleanup(execution.get(env_id))


@pytest.mark.parametrize("source_kind", ["fixture", "cached_desktop_project"])
def test_real_desktop_offline_tests_never_start_gui_and_report_partial_scope(
    service, source_kind,  # noqa: F811
):
    import asyncio

    from test_desktop_projects import write_source
    from test_repository_analysis import Engine, ReviewModel

    from repowatch.repair_workspace import source_tree
    from repowatch.repository_analysis import Analyses, run_analysis

    execution, repo, _ = service
    cached = os.getenv("REPOWATCH_DESKTOP_VALIDATION_SOURCE")
    if source_kind == "cached_desktop_project" and not cached:
        pytest.skip("Explicit cached desktop path required for this local validation")

    class LocalDesktopSource:
        def download(self, repo, account_id, version, destination, check):
            check()
            if source_kind == "fixture":
                write_source(destination)
            else:
                source = Path(cached).resolve()
                workspace = Path(__file__).resolve().parents[1] / "data/repowatch-workspaces"
                assert source.is_relative_to(workspace) and source.name == "source"
                source_tree(source, check)
                shutil.copytree(source, destination)
            return SHA, discover(destination)

    class LocalReview(Engine):
        async def analyze(self, context, event):
            return await run_analysis(ReviewModel(findings=False), context, event)

    execution.source = LocalDesktopSource()
    execution.runtime = DockerRuntime(execution.store.setting("execution_namespace"))
    analyses = Analyses(execution, LocalReview())
    assert execution.runtime.readiness()["ready"]
    env = execution.create(repo["id"], auto_run=True)
    try:
        execution.tick()
        current = execution.get(env["id"])
        assert current["pipeline"]["error"] == "desktop_confirmation_required"
        config = {key: current["config"][key] for key in (
            "language", "install", "test", "start", "port", "install_network", "run_mode",
        )}
        execution.configure(env["id"], current["version"], config, desktop_offline_confirmed=True)
        for _ in range(8):
            execution.tick()
            asyncio.run(analyses.tick())
        result = execution.get(env["id"])
        diagnostic = {"error": result["error"], "pipeline_state": result["pipeline"]["state"],
                      "scope": (result["test_result"] or {}).get("desktop_scope")}
        assert result["pipeline"]["state"] == "completed", diagnostic
        assert result["test_result"]["exit_code"] == 0, diagnostic
        assert result["test_result"]["desktop_scope"]["executed"] > 0, diagnostic
        assert not result["test_result"]["desktop_scope"]["complete"]
        assert result["url"] is None and result["access_result"] is None
        assert [job["action"] for job in reversed(result["jobs"])] == ["download", "setup", "test"]
        report = analyses.list(env["id"])[0]
        assert report["status"] == "completed" and report["findings"] == []
        assert report["report"]["execution_scope"]["uncovered"]
        print("DESKTOP_VALIDATION", source_kind,
              {key: result["test_result"]["desktop_scope"][key]
               for key in ("total", "executed", "skipped", "complete")})
    finally:
        template = execution.get(env["id"])
        with execution.store.connection() as db:
            caches = [row[0] for row in db.execute("SELECT id FROM dependency_snapshots")]
        for cache_id in caches:
            execution.runtime.delete_dependencies(template, cache_id)
        execution.runtime.cleanup(template)
