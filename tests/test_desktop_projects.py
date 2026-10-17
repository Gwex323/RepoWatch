"""Desktop proposals are static, partial, owner-approved and container-only."""

import json

import pytest
from fastapi.testclient import TestClient
from test_execution import service  # noqa: F401
from test_execution_pipeline import PipelineRuntime, drain, tick
from test_github_accounts import MemoryVault, Remote
from test_repository_analysis import Engine

from repowatch.app import create_app
from repowatch.desktop_projects import desktop_profile, offline_test_scope
from repowatch.execution_pipeline import passing_test
from repowatch.repository_analysis import Analyses
from repowatch.repository_source import discover
from repowatch.store import Conflict

FILES = {
    "app.py": "print('ok')\nimport tkinter as tk\n",
    "core.py": "VALUE = 42\n",
    "tests/test_core.py": "import unittest\nimport core\nclass Core(unittest.TestCase):\n"
                          "    def test_value(self): self.assertEqual(core.VALUE, 42)\n",
    "tests/test_gui.py": "import unittest\nimport app\n",
    "tests/test_windows.py": "import unittest\nimport winreg\n",
}


def write_source(root, files=None):
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (files or FILES).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


class DesktopSource:
    revision = "d" * 40

    def download(self, repo, account_id, version, destination, check):
        check()
        write_source(destination)
        return self.revision, discover(destination)


@pytest.fixture
def desktop(service):  # noqa: F811
    execution, repo, _ = service
    execution.source = DesktopSource()
    execution.runtime = PipelineRuntime()
    analyses = Analyses(execution, Engine())
    env = execution.create(repo["id"], auto_run=True)
    tick(execution)
    return execution, repo, execution.runtime, analyses, execution.get(env["id"])


def approved(execution, env, **changes):
    config = {key: env["config"][key] for key in (
        "language", "install", "test", "start", "port", "install_network", "run_mode",
    )}
    return execution.configure(env["id"], env["version"], {**config, **changes},
                               desktop_offline_confirmed=True)


def test_stdlib_desktop_without_manifest_proposes_partial_unittest_and_no_gui(tmp_path):
    root = write_source(tmp_path / "source")
    (root / "requirements-build.txt").write_text("pyinstaller==6.19.0\n")
    c = discover(root)
    assert c["detected"] and c["run_mode"] == "test_only" and not c["desktop_approved"]
    assert c["install"] == "python -m venv /workspace/home/venv"
    assert c["start"] == "" and c["install_network"] is False
    assert "pip" not in c["install"] and "pyinstaller" not in c["install"]
    p = c["project_profile"]
    assert [item["module"] for item in p["selected"]] == ["test_core"]
    assert {item["path"] for item in p["excluded"]} == {
        "tests/test_gui.py", "tests/test_windows.py",
    }
    assert "test_gui" not in c["test"] and "test_windows" not in c["test"]
    assert not p["complete"] and p["uncovered"]


@pytest.mark.parametrize("dependency,reason", [
    ("import tkinter", "gui"), ("import winreg", "windows_native"),
    ("import imaginary_dependency", "unknown_dependency"),
    ("import importlib\nimportlib.import_module(variable)", "dynamic_import"),
])
def test_transitive_imports_exclude_unsafe_or_unconfirmed_test_modules(
    tmp_path, dependency, reason,
):
    root = write_source(tmp_path)
    (root / "helper.py").write_text(dependency)
    (root / "tests/test_core.py").write_text("import unittest\nimport helper\n")
    p = desktop_profile(root)
    assert p["selected"] == []
    assert reason in next(x for x in p["excluded"] if x["path"] == "tests/test_core.py")["reasons"]


def test_relative_package_imports_are_followed_without_running_source(tmp_path):
    root = write_source(tmp_path)
    write_source(root, {"widget/__init__.py": "from . import ui\n",
                        "widget/ui.py": "import tkinter\n",
                        "tests/test_core.py": "import unittest\nimport widget\n"})
    p = desktop_profile(root)
    assert not p["selected"]
    assert "gui" in next(x for x in p["excluded"] if x["path"] == "tests/test_core.py")["reasons"]
    sentinel = root / "MUST_NOT_RUN"
    (root / "app.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\nimport tkinter",
    )
    assert discover(root)["detected"] and not sentinel.exists()


def test_unknown_runtime_dependencies_do_not_get_fake_stdlib_setup(tmp_path):
    root = write_source(tmp_path)
    (root / "app.py").write_text("import tkinter\nimport imaginary_dependency\n")
    result = discover(root)
    assert not result["detected"] and result["reason"] == "desktop_dependencies_required"
    assert not result["install"] and not result["project_profile"]["runtime_stdlib_only"]


def test_non_gui_scripts_and_mixed_languages_are_not_guessed(tmp_path):
    root = write_source(tmp_path)
    (root / "app.py").write_text("print('not a desktop declaration')\n")
    assert not discover(root)["detected"]
    (root / "app.py").write_text("import tkinter\n")
    (root / "requirements.txt").write_text("")
    (root / "package.json").write_text('{"scripts":{}}')
    assert not discover(root)["detected"]


def test_invalid_test_paths_and_selection_limits_are_explicit(tmp_path, monkeypatch):
    root = write_source(tmp_path)
    (root / "tests/test-bad.py").write_text("import unittest")
    monkeypatch.setattr("repowatch.desktop_projects.MAX_TEST_MODULES", 0)
    p = desktop_profile(root)
    assert not p["selected"]
    assert any("unsupported_module_path" in x["reasons"] for x in p["excluded"])
    assert any("selection_limit" in x["reasons"] for x in p["excluded"])


def test_static_scan_limits_and_stdlib_shadowing_fail_conservatively(tmp_path, monkeypatch):
    root = write_source(tmp_path)
    (root / "json.py").write_text("VALUE=1")
    p = desktop_profile(root)
    assert not p["runtime_stdlib_only"] and not p["selected"]
    monkeypatch.setattr("repowatch.desktop_projects.MAX_MODULES", 1)
    assert desktop_profile(root) is None


def test_download_waits_for_specific_desktop_consent_and_never_starts_gui(desktop):
    execution, _, runtime, analyses, env = desktop
    assert env["pipeline"]["state"] == "waiting_configuration"
    assert env["pipeline"]["error"] == "desktop_confirmation_required"
    assert runtime.calls == [] and not env["installed"]
    with pytest.raises(Conflict, match="pipeline_busy"):
        execution.action(env["id"], env["version"], "setup")
    config = {key: env["config"][key] for key in (
        "language", "install", "test", "start", "port", "install_network", "run_mode",
    )}
    with pytest.raises(Conflict, match="desktop_confirmation_required"):
        execution.configure(env["id"], env["version"], config)
    with pytest.raises(Conflict, match="desktop_test_only_required"):
        approved(execution, env, run_mode="service")
    assert analyses.list(env["id"]) == []


def test_confirmed_pipeline_only_sets_up_tests_and_analyzes_with_uncovered_limits(desktop):
    execution, _, runtime, analyses, env = desktop
    approved(execution, env)
    final = drain((execution, None, runtime, analyses), env["id"])
    assert final["pipeline"]["state"] == "completed"
    assert runtime.calls == ["setup", "test"]
    assert final["url"] is None and final["access_result"] is None
    scope = final["test_result"]["desktop_scope"]
    assert scope["executed"] == 1 and scope["skipped"] == 0 and not scope["complete"]
    report = analyses.list(env["id"])[0]["report"]
    assert report["execution_scope"] == scope
    assert report["execution_scope"]["uncovered"]
    with pytest.raises(Conflict, match="desktop_service_unsupported"):
        execution.action(env["id"], final["version"], "start")


@pytest.mark.parametrize("log", ["Ran 0 tests\nOK", "Ran 2 tests\nOK (skipped=2)", "no summary"])
def test_zero_unknown_or_all_skipped_tests_cannot_pass_or_trigger_analysis(desktop, log):
    execution, _, runtime, analyses, env = desktop
    runtime.test_log = log
    approved(execution, env)
    final = drain((execution, None, runtime, analyses), env["id"])
    assert final["test_result"]["exit_code"] == 5 and final["state"] == "test_failed"
    assert final["pipeline"]["error"] == "desktop_tests_not_executed"
    assert runtime.calls == ["setup", "test"] and not analyses.list(env["id"])


def test_scopes_record_skips_and_custom_commands_without_claiming_candidate_coverage(tmp_path):
    c = discover(write_source(tmp_path))
    scope = offline_test_scope(c, "Ran 5 tests\nOK (skipped=2)")
    assert scope["total"] == 5 and scope["executed"] == 3 and scope["skipped"] == 2
    custom = offline_test_scope({**c, "test": "custom command"}, "Ran 1 test\nOK")
    assert custom["selection"] == "custom" and custom["selected"] == []
    assert not custom["complete"] and custom["uncovered"]


def test_legacy_test_success_without_desktop_scope_is_not_passing_evidence(desktop):
    _, _, _, _, env = desktop
    raw = {**env, "config": json.dumps({**env["config"], "desktop_approved": True}),
           "installed": 1, "test_result": json.dumps({"exit_code": 0,
           "revision": env["revision"], "command": env["config"]["test"]})}
    assert not passing_test(raw)


def test_saved_desktop_approval_reuses_mode_but_not_a_stale_generated_selection(desktop):
    execution, repo, runtime, analyses, env = desktop
    approved(execution, env)
    drain((execution, repo, runtime, analyses), env["id"])
    execution.source.revision = "e" * 40
    new = execution.create(repo["id"], auto_run=True)
    tick(execution)
    new = execution.get(new["id"])
    assert new["config"]["desktop_approved"] is True
    assert new["config"]["project_profile"]["kind"] == "python_desktop"
    assert new["config"]["run_mode"] == "test_only"
    assert new["pipeline"]["state"] == "running" and new["pipeline"]["stage"] == "setup"


def test_detecting_cached_legacy_download_only_saves_proposal_and_does_not_execute(desktop):
    execution, _, runtime, _, env = desktop
    legacy = {k: env["config"][k] for k in ("language", "port", "install_network")}
    legacy.update(install="", test="", start="", detected=False, reason="ambiguous_or_unsupported")
    with execution.store.connection() as db:
        db.execute("UPDATE environments SET config=?,state='config_required' WHERE id=?",
                   (json.dumps(legacy), env["id"]))
    new = execution.detect_configuration(env["id"], env["version"])
    assert new["config"]["project_profile"]["kind"] == "python_desktop"
    assert not new["config"]["desktop_approved"]
    assert len(new["jobs"]) == 1 and runtime.calls == []
    assert new["pipeline"]["error"] == "desktop_confirmation_required"
    with pytest.raises(Conflict, match="configuration_detection_not_applicable"):
        execution.detect_configuration(new["id"], new["version"])


def test_detect_api_requires_owner_csrf_and_confirmation_cannot_be_forged_in_metadata(desktop):
    execution, _, _, _, env = desktop
    legacy = {k: env["config"][k] for k in ("language", "port", "install_network")}
    legacy.update(install="", test="", start="", detected=False)
    with execution.store.connection() as db:
        db.execute("UPDATE environments SET config=? WHERE id=?", (json.dumps(legacy), env["id"]))
    app = create_app(execution.store.path, start_worker=False, github_client=Remote().client,
                     credential_vault=MemoryVault())
    app.state.store.setup_password("test-only-owner-password")
    url = f"/api/environments/{env['id']}/configuration/detect"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post(url, json={"version": env["version"]}).status_code == 401
        login = client.post("/api/login", json={"password": "test-only-owner-password"})
        csrf = login.json()["csrf"]
        assert client.post(url, json={"version": env["version"]}).status_code == 403
        response = client.post(url, json={"version": env["version"]},
                               headers={"x-csrf-token": csrf})
        assert response.status_code == 200 and not response.json()["busy"]
        c = response.json()["config"]
        body = {k: c[k] for k in ("language", "install", "test", "start", "port", "install_network",
                                  "run_mode")}
        body.update(version=response.json()["version"], project_profile=c["project_profile"])
        assert client.post(url.removesuffix("/detect"), json=body,
                           headers={"x-csrf-token": csrf}).status_code == 422


def test_manual_actions_also_require_approval_and_cannot_launch_desktop(desktop):
    execution, _, _, _, env = desktop
    execution.action(env["id"], env["version"], "stop")
    tick(execution)
    current = execution.get(env["id"])
    with pytest.raises(Conflict, match="desktop_confirmation_required"):
        execution.action(current["id"], current["version"], "setup")


def test_test_directory_module_collisions_are_not_guessed(tmp_path):
    root = write_source(tmp_path)
    (root / "tests/core.py").write_text("import tkinter\n")
    profile = desktop_profile(root)
    assert profile["selected"] == []
    assert any("ambiguous_local_import" in x["reasons"] for x in profile["excluded"])


def test_symlink_modules_are_not_read_by_static_discovery(tmp_path, monkeypatch):
    from pathlib import Path

    root = write_source(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == "app.py" or original(p))
    assert desktop_profile(root) is None


def test_qualified_submodule_import_also_checks_package_initialization(tmp_path):
    root = write_source(tmp_path)
    write_source(root, {"widget/__init__.py": "import tkinter\n",
                        "widget/safe.py": "VALUE=42\n",
                        "tests/test_core.py": "import unittest\nimport widget.safe\n"})
    profile = desktop_profile(root)
    assert not profile["selected"]
    assert "gui" in next(x for x in profile["excluded"]
                         if x["path"] == "tests/test_core.py")["reasons"]
