"""Mock-only execution pipeline, configuration retention and cancellation contracts."""

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from test_execution import Runtime, Source, prepare, service  # noqa: F401
from test_repository_analysis import Engine

from repowatch import report_scheduler
from repowatch.execution import Execution
from repowatch.repository_analysis import Analyses
from repowatch.repository_source import discover
from repowatch.store import Store


class PipelineRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.codes = {}
        self.test_log = "Ran 1 test\nOK"

    def run(self, env, action, check):
        result = super().run(env, action, check)
        if action == "start":
            return result
        return self.codes.get(action, 0), self.test_log if action == "test" else "fixture setup"


@pytest.fixture
def pipeline(service):  # noqa: F811
    execution, repo, _ = service
    runtime = PipelineRuntime()
    execution.runtime = runtime
    execution.probe_access = lambda port: {"status_code": 200, "path": "/", "checked_at": 1}
    analyses = Analyses(execution, Engine())
    return execution, repo, runtime, analyses


def tick(execution):
    with patch("repowatch.execution.socket.create_connection"):
        return execution.tick()


def drain(pipeline, env_id, count=8):
    execution, _, _, analyses = pipeline
    for _ in range(count):
        tick(execution)
        asyncio.run(analyses.tick())
    return execution.get(env_id)


def configure(execution, env, **changes):
    config = {key: env["config"][key] for key in (
        "language", "install", "test", "start", "port", "install_network",
    )}
    return execution.configure(env["id"], env["version"], {**config, **changes})


def test_download_pipeline_orders_steps_and_defers_analysis_until_http_success(pipeline):
    execution, repo, runtime, analyses = pipeline
    env = execution.create(repo["id"], auto_run=True)
    assert execution.create(repo["id"], auto_run=True)["id"] == env["id"]
    for action in ("download", "setup", "test", "start"):
        tick(execution)
        current = execution.get(env["id"])
        assert current["jobs"][1 if action != "start" else 0]["action"] == action
        asyncio.run(analyses.tick())
        assert not analyses.list(env["id"])
    result = drain(pipeline, env["id"], 2)
    assert runtime.calls == ["setup", "test", "start"]
    assert result["pipeline"]["state"] == "completed"
    assert result["access_result"]["status_code"] == 200
    assert result["url"] == "http://127.0.0.2:49152"
    assert len(analyses.list(env["id"])) == 1
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM repair_tasks").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["setup", "test", "empty_tests"])
def test_pipeline_failure_never_starts_or_analyzes(pipeline, failure):
    execution, repo, runtime, analyses = pipeline
    if failure == "empty_tests":
        runtime.test_log = "Ran 0 tests\nOK"
    else:
        runtime.codes[failure] = 1
    env = execution.create(repo["id"], auto_run=True)
    result = drain(pipeline, env["id"])
    assert result["pipeline"]["state"] == "failed" and "start" not in runtime.calls
    assert not analyses.list(env["id"])
    if failure == "empty_tests":
        assert result["test_result"]["exit_code"] != 0


def test_start_config_change_preserves_setup_tests_and_valid_test_log_evidence(pipeline):
    execution, repo, runtime, analyses = pipeline
    env = prepare((execution, repo, runtime))
    execution.action(env["id"], env["version"], "test")
    tick(execution)
    tested = execution.get(env["id"])
    updated = configure(execution, tested, start="python app.py --new", port=8888)
    assert updated["installed"] and updated["test_result"] == tested["test_result"]
    assert updated["image_id"] == tested["image_id"]
    execution.continue_pipeline(updated["id"], updated["version"])
    result = drain(pipeline, updated["id"])
    assert runtime.calls == ["setup", "test", "start"]
    assert result["pipeline"]["state"] == "completed"
    with execution.store.connection() as db:
        report = db.execute("SELECT context FROM repository_analyses WHERE id=?",
                            (result["pipeline"]["analysis_id"],)).fetchone()
    assert {job["action"] for job in json.loads(report[0])["jobs"]} == {"setup", "test", "start"}


@pytest.mark.parametrize("change", ["test", "install", "language", "install_network"])
def test_config_invalidation_is_limited_to_affected_evidence(pipeline, change):
    execution, repo, runtime, _ = pipeline
    env = prepare((execution, repo, runtime))
    execution.action(env["id"], env["version"], "test")
    tick(execution)
    env = execution.get(env["id"])
    value = {"test": "echo changed", "install": "echo changed", "language": "go",
             "install_network": True}[change]
    updated = configure(execution, env, **{change: value})
    assert updated["test_result"] is None
    assert updated["installed"] == (change == "test")


def test_missing_start_waits_then_saving_start_resumes_without_retesting(pipeline):
    execution, repo, runtime, analyses = pipeline
    source = execution.source

    class NoStart:
        def download(self, *args):
            revision, config = source.download(*args)
            return revision, {**config, "start": ""}

    execution.source = NoStart()
    env = execution.create(repo["id"], auto_run=True)
    result = drain(pipeline, env["id"], 4)
    assert result["pipeline"]["state"] == "waiting_configuration"
    assert result["pipeline"]["error"] == "pipeline_start_command_required"
    assert result["test_result"]["exit_code"] == 0
    assert not analyses.list(env["id"])
    configured = configure(execution, result, start="python app.py")
    assert configured["test_result"] == result["test_result"] and configured["installed"]
    result = drain(pipeline, env["id"])
    assert result["pipeline"]["state"] == "completed"
    assert runtime.calls == ["setup", "test", "start"]


def test_explicit_test_only_mode_does_not_fake_service_start(pipeline):
    execution, repo, runtime, _ = pipeline
    env = prepare((execution, repo, runtime))
    env = configure(execution, env, start="", run_mode="test_only")
    execution.continue_pipeline(env["id"], env["version"])
    result = drain(pipeline, env["id"])
    assert runtime.calls == ["setup", "test"]
    assert result["pipeline"]["state"] == "completed"
    assert result["access_result"] is None and result["url"] is None


def test_cancel_after_download_revokes_next_queued_step_and_survives_restart(pipeline):
    execution, repo, runtime, _ = pipeline
    env = execution.create(repo["id"], auto_run=True)
    tick(execution)
    env = execution.get(env["id"])
    execution.action(env["id"], env["version"], "stop")
    restarted = Execution(Store(execution.store.path), execution.github,
                          runtime=runtime, source=Source())
    Analyses(restarted, Engine())
    for _ in range(3):
        tick(restarted)
    result = restarted.get(env["id"])
    assert result["pipeline"]["state"] == "cancelled"
    assert not runtime.calls


def test_pipeline_waits_for_docker_without_repeated_project_execution(pipeline):
    execution, repo, runtime, _ = pipeline
    runtime.fail = True
    env = execution.create(repo["id"], auto_run=True)
    tick(execution)
    tick(execution)
    waiting = execution.get(env["id"])
    assert waiting["pipeline"]["state"] == "waiting_environment"
    jobs = len(waiting["jobs"])
    for _ in range(3):
        tick(execution)
    assert len(execution.get(env["id"])["jobs"]) == jobs and not runtime.calls
    runtime.fail = False
    with execution.store.connection() as db:
        db.execute("UPDATE execution_pipelines SET next_check=0")
    assert drain(pipeline, env["id"])["pipeline"]["state"] == "completed"


def test_http_failure_never_reaches_analysis(pipeline):
    execution, repo, _, analyses = pipeline
    env = execution.create(repo["id"], auto_run=True)
    for _ in range(3):
        tick(execution)
    execution.probe_access = lambda port: None
    with patch("repowatch.execution.time.monotonic", side_effect=[0, 121]):
        tick(execution)
    result = execution.get(env["id"])
    assert result["pipeline"]["state"] == "failed"
    assert result["error"] == "start_not_listening" and result["url"] is None
    assert not analyses.list(env["id"])


def test_model_disabled_waits_instead_of_faking_completed_analysis(pipeline):
    execution, repo, _, analyses = pipeline
    analyses.engine.enabled = False
    env = execution.create(repo["id"], auto_run=True)
    waiting = drain(pipeline, env["id"])
    assert waiting["pipeline"]["state"] == "waiting_model"
    analyses.engine.enabled = True
    assert drain(pipeline, env["id"])["pipeline"]["state"] == "completed"


def test_explicit_cancel_during_analysis_cannot_be_overwritten_by_late_model_result(pipeline):
    execution, repo, _, analyses = pipeline
    env = execution.create(repo["id"], auto_run=True)
    for _ in range(5):
        tick(execution)
    entered = asyncio.Event()
    completed = []
    original = analyses.engine.analyze

    async def stubborn(context, event):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            completed.append(True)
            return await original(context, event)

    analyses.engine.analyze = stubborn

    async def run():
        task = asyncio.create_task(analyses.tick())
        await asyncio.wait_for(entered.wait(), 3)
        latest = execution.get(env["id"])
        execution.action(env["id"], latest["version"], "stop")
        await asyncio.wait_for(task, 3)

    asyncio.run(run())
    result = execution.get(env["id"])
    assert result["pipeline"]["state"] == "cancelled" and completed
    with execution.store.connection() as db:
        analysis = db.execute(
            "SELECT status,error,lease FROM repository_analyses"
        ).fetchone()
        recovery = db.execute(
            "SELECT state,stop_reason,retry_after FROM analysis_report_recovery"
        ).fetchone()
        cancellation = db.execute(
            "SELECT kind,agent FROM analysis_events "
            "WHERE kind='analysis_cancelled'",
        ).fetchone()
        assert db.execute("SELECT count(*) FROM analysis_findings").fetchone()[0] == 0
    assert dict(analysis) == {
        "status": "cancelled", "error": "analysis_cancelled", "lease": 0,
    }
    assert dict(recovery) == {
        "state": "stopped", "stop_reason": "pipeline_stopped", "retry_after": 0,
    }
    assert dict(cancellation) == {
        "kind": "analysis_cancelled", "agent": "pipeline_stopped",
    }


def test_environment_stop_cancels_an_orphan_analysis_and_its_recovery(pipeline):
    execution, repo, runtime, analyses = pipeline
    env = prepare((execution, repo, runtime))
    execution.action(env["id"], env["version"], "test")
    tick(execution)
    env = execution.get(env["id"])
    analysis_id = analyses.enqueue(env["id"], env["version"], manual=True)
    with execution.store.connection() as db:
        db.execute(
            "UPDATE repository_analyses SET status='running',lease=? WHERE id=?",
            (time.time() + 300, analysis_id),
        )
        report_scheduler.begin(db, analysis_id, time.time())
    execution.action(env["id"], env["version"], "stop")
    with execution.store.connection() as db:
        analysis = db.execute(
            "SELECT status,error,lease FROM repository_analyses WHERE id=?", (analysis_id,),
        ).fetchone()
        recovery = db.execute(
            "SELECT state,stop_reason,last_error,retry_after "
            "FROM analysis_report_recovery WHERE analysis_id=?", (analysis_id,),
        ).fetchone()
        cancellation = db.execute(
            "SELECT kind,agent FROM analysis_events "
            "WHERE analysis_id=? AND kind='analysis_cancelled'",
            (analysis_id,),
        ).fetchone()
    assert dict(analysis) == {
        "status": "cancelled", "error": "analysis_cancelled", "lease": 0,
    }
    assert dict(recovery) == {
        "state": "stopped", "stop_reason": "pipeline_stopped",
        "last_error": "analysis_cancelled", "retry_after": 0,
    }
    assert dict(cancellation) == {
        "kind": "analysis_cancelled", "agent": "pipeline_stopped",
    }


def test_saved_owner_start_configuration_is_used_on_later_download(pipeline):
    execution, repo, _, _ = pipeline
    env = prepare((execution, repo, execution.runtime))
    configure(execution, env, start="DEMO_MODE=true python app.py", port=8888)
    downloaded = execution.create(repo["id"], auto_run=False)
    tick(execution)
    result = execution.get(downloaded["id"])
    assert result["config"]["start"] == "DEMO_MODE=true python app.py"
    assert result["config"]["port"] == 8888


def test_fastapi_entry_discovery_does_not_execute_downloaded_code(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/main.py").write_text(
        "raise RuntimeError('must never run on host')\nfrom fastapi import FastAPI as API\n"
        "app = API()\n",
    )
    config = discover(tmp_path)
    assert config["start"] == (
        "/workspace/home/venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
    )



def test_save_and_continue_atomically_skips_valid_setup_and_tests(pipeline):
    execution, repo, runtime, analyses = pipeline
    env = prepare((execution, repo, runtime))
    execution.action(env["id"], env["version"], "test")
    tick(execution)
    env = execution.get(env["id"])
    config = dict(env["config"], start="DEMO_MODE=true python app.py")
    saved = execution.configure(env["id"], env["version"], config, continue_pipeline=True)
    assert saved["installed"] and saved["test_result"] == env["test_result"]
    assert saved["pipeline"]["stage"] == "start"
    assert saved["jobs"][0]["action"] == "start" and saved["jobs"][0]["state"] == "queued"
    assert not asyncio.run(analyses.tick())
    assert not analyses.list(env["id"])
    result = drain(pipeline, env["id"])
    assert result["pipeline"]["state"] == "completed"
    assert runtime.calls == ["setup", "test", "start"]


def test_saving_start_metadata_alone_does_not_trigger_premature_model_analysis(pipeline):
    execution, repo, runtime, analyses = pipeline
    env = prepare((execution, repo, runtime))
    execution.action(env["id"], env["version"], "test")
    tick(execution)
    env = execution.get(env["id"])
    saved = configure(execution, env, start="DEMO_MODE=true python app.py")
    assert saved["test_result"] == env["test_result"]
    assert not asyncio.run(analyses.tick())
    assert not analyses.list(env["id"])
    assert runtime.calls == ["setup", "test"]



def test_api_download_default_manual_optout_and_atomic_save_continue(tmp_path):
    from fastapi.testclient import TestClient
    from test_github_accounts import TOKEN_A, MemoryVault, Remote

    from repowatch.app import create_app

    runtime = PipelineRuntime()
    app = create_app(tmp_path / "pipeline-api.db", start_worker=False,
                     github_client=Remote().client, credential_vault=MemoryVault(),
                     execution_runtime=runtime, repository_source=Source(),
                     analysis_engine=Engine())
    execution = app.state.execution
    with TestClient(app, base_url="http://localhost") as client:
        session = client.post("/api/setup", json={"password": "owner-pipeline-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        client.post("/api/github/accounts", headers=headers,
                    json={"username": "alice", "token": TOKEN_A})
        repo = client.post("/api/repositories", headers=headers,
                           json={"url": "https://github.com/alice/project"}).json()
        path = f"/api/repositories/{repo['id']}/environments"
        assert client.post(path, headers=headers, json={"auto_run": 1}).status_code == 422
        created = client.post(path, headers=headers, json={}).json()
        assert created["pipeline"]["stage"] == "download"
        assert client.post(path, headers=headers, json={}).json()["id"] == created["id"]
        execution.tick()
        env = execution.get(created["id"])
        stopped = client.post(f"/api/environments/{env['id']}/stop", headers=headers,
                              json={"version": env["version"]})
        assert stopped.status_code == 202
        execution.tick()
        manual = client.post(path, headers=headers, json={"auto_run": False}).json()
        assert manual["pipeline"] is None
        execution.tick()
        env = execution.get(manual["id"])
        for action in ("setup", "test"):
            execution.action(env["id"], env["version"], action)
            execution.tick()
            env = execution.get(env["id"])
        config = {key: env["config"][key] for key in (
            "language", "install", "test", "start", "port", "install_network",
        )}
        body = {**config, "version": env["version"], "start": "DEMO_MODE=true python app.py",
                "continue_pipeline": True}
        url = f"/api/environments/{env['id']}/configuration"
        assert client.post(url, json=body).status_code == 403
        saved = client.post(url, headers=headers, json=body)
        assert saved.status_code == 200
        current = saved.json()
        assert current["installed"] and current["test_result"] == env["test_result"]
        assert current["pipeline"]["stage"] == "start"
        assert client.post(url, headers=headers, json=body).status_code == 409
        assert runtime.calls == ["setup", "test"]



def test_access_probe_releases_bare_socket_before_single_threaded_http(pipeline):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    execution, repo, runtime, _ = pipeline
    env = execution.create(repo["id"], auto_run=True)
    for _ in range(3):
        tick(execution)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"single-threaded-fixture")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.2", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original_run = runtime.run

    def run(env, action, check):
        if action == "start":
            check()
            runtime.calls.append(action)
            return server.server_address[1]
        return original_run(env, action, check)

    runtime.run = run
    execution.probe_access = Execution.probe_access
    try:
        assert execution.tick()
        result = execution.get(env["id"])
        assert result["access_result"]["status_code"] == 200
        assert result["pipeline"]["state"] == "waiting_analysis"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
