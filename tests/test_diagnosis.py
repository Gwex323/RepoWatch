import asyncio
import json
import time

import pytest
from agents import Model, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from test_execution import Runtime, Source, service  # noqa: F401
from test_github_accounts import TOKEN_A, MemoryVault, Remote

from repowatch.app import create_app
from repowatch.diagnosis import (
    Diagnoser,
    agents_for,
    local_report,
    manifest_context,
    redact,
    run_agents,
)
from repowatch.store import Conflict

DNS_LOG = (
    "WARNING NameResolutionError: Failed to resolve 'pypi.org'\n"
    "ERROR: Could not find a version that satisfies the requirement fastapi>=0.115.0\n"
    "ERROR: No matching distribution found for fastapi>=0.115.0\n"
)
UNKNOWN_LOG = "ERROR build configuration missing"


def failure(execution_fixture, log=DNS_LOG):
    execution, repo, runtime = execution_fixture
    env = execution.create(repo["id"])
    execution.tick()
    env = execution.get(env["id"])
    runtime.run = lambda *_: (1, log)
    execution.action(env["id"], env["version"], "setup")
    execution.tick()
    return execution.get(env["id"])


def test_network_disabled_is_diagnosed_before_misleading_package_error(service):  # noqa: F811
    execution, _, _ = service
    env = failure(service)
    diagnosis = env["diagnosis"]
    assert diagnosis["state"] == "complete" and diagnosis["applicable"]
    report = diagnosis["report"]
    assert report["source"] == "local_rules"
    assert report["code"] == "install_network_disabled"
    assert report["patch"] == {"install_network": True}
    assert "pypi.org" in report["evidence"][0]
    assert report["handoffs"] == []
    assert not env["config"]["install_network"]
    assert execution.store.dashboard()["runs"] == []
    assert env["state"] == "setup_failed" and not env["installed"]


def test_apply_retries_atomically_and_does_not_automatically_repeat(service):  # noqa: F811
    execution, _, runtime = service
    env = failure(service)
    job_id = env["diagnosis"]["job_id"]
    result = execution.apply_diagnosis(env["id"], job_id, env["version"])
    assert result["busy"] and result["config"]["install_network"]
    assert not result["diagnosis"]["applicable"]
    with pytest.raises(Conflict):
        execution.apply_diagnosis(env["id"], job_id, env["version"])
    runtime.run = lambda *_: (0, "Installed")
    execution.tick()
    result = execution.get(env["id"])
    assert result["installed"] and result["state"] == "ready"
    assert not result["diagnosis"]["current"]
    assert execution.tick() is False
    with pytest.raises(Conflict, match="diagnosis_stale"):
        execution.apply_diagnosis(env["id"], job_id, result["version"])


def test_config_change_invalidates_advice_and_snapshot_is_immutable(service):  # noqa: F811
    execution, _, _ = service
    env = failure(service)
    job_id = env["diagnosis"]["job_id"]
    execution.configure(env["id"], env["version"], {**env["config"], "install": "echo changed"})
    result = execution.get(env["id"])
    assert not result["diagnosis"]["current"]
    with pytest.raises(Conflict, match="diagnosis_stale"):
        execution.apply_diagnosis(env["id"], job_id, result["version"])
    with execution.store.connection() as db:
        original = json.loads(db.execute(
            "SELECT context FROM execution_jobs WHERE id=?", (job_id,),
        ).fetchone()["context"])
    assert "echo changed" not in original["config"]["install"]


def test_legacy_failure_backfill_does_not_guess_after_configuration_changes(service):  # noqa: F811
    execution, _, _ = service
    env = failure(service)
    with execution.store.connection() as db:
        db.execute("DELETE FROM execution_diagnoses")
        db.execute("UPDATE execution_jobs SET context=NULL")
    execution.diagnoses.backfill()
    assert execution.get(env["id"])["diagnosis"]["report"]["code"] == "install_network_disabled"
    with execution.store.connection() as db:
        db.execute("DELETE FROM execution_diagnoses")
    execution.configure(env["id"], env["version"], {**env["config"], "install_network": True})
    execution.diagnoses.backfill()
    assert execution.get(env["id"])["diagnosis"] is None


def test_enabled_network_and_test_network_errors_do_not_get_false_toggle_advice():
    for action, network in (("setup", True), ("test", False), ("start", False)):
        report = local_report({
            "log": DNS_LOG, "config": {"install_network": network}, "action": action,
        })
        assert report["code"] == "network_unreachable" and not report["patch"]


def test_zero_tests_reports_framework_mismatch_without_claiming_success():
    report = local_report({
        "log": "Ran 0 tests in 0.000s\n\nNO TESTS RAN\n",
        "config": {"test": "python -m unittest discover -s tests"},
        "action": "test",
        "manifests": {"pytest.ini": "[pytest]\ntestpaths = tests\n"},
    })
    assert report["code"] == "no_tests_collected"
    assert "pytest" in report["steps"][0]
    assert "requirements-dev.txt" in report["steps"][1]
    assert not report["patch"]


def test_zero_tests_without_framework_evidence_does_not_invent_pytest():
    report = local_report({
        "log": "collected 0 items", "config": {}, "action": "test", "manifests": {},
    })
    assert report["code"] == "no_tests_collected"
    assert all("pytest" not in step for step in report["steps"])


@pytest.mark.parametrize("log,code", [
    ("ERROR: package requires Python >= 3.14", "runtime_version"),
    ("ERROR: ResolutionImpossible", "dependency_conflict"),
    ("Error: Permission denied: '/usr/local/lib'", "write_permission"),
    ("ERROR: gcc not found", "compiler_missing"),
    ("npm ci requires lock file", "lockfile_mismatch"),
    ("Unknown installer error", "unknown"),
])
def test_other_failures_remain_unverified_advice(log, code):
    result = local_report({"log": log, "config": {}, "action": "setup"})
    assert result["code"] == code and result["source"] == "local_rules"
    assert result["confidence"] == "uncertain"
    assert not result["patch"]


def test_manifests_are_bounded_and_credentials_are_redacted(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "https://user:privatepass@example.com/pkg\n"
        "api_key=sk-secretvalue\nAuthorization: Bearer hiddenvalue\n"
        + "x" * 12000,
    )
    (tmp_path / ".env").write_text("SECRET_ENV=must_not_read")
    result = manifest_context(tmp_path)
    text = json.dumps(result)
    for value in ("privatepass", "sk-secretvalue", "hiddenvalue", "must_not_read"):
        assert value not in text
    assert len(result["requirements.txt"]) <= 8000
    assert "[REDACTED]" in text
    assert "ghp_abcdefgh" not in redact("error ghp_abcdefgh")


def test_model_config_is_explicit_and_does_not_expose_keys(monkeypatch):
    for key in ("ENABLED", "MODEL", "API_KEY", "BASE_URL", "API"):
        monkeypatch.delenv("REPOWATCH_DIAGNOSIS_" + key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    assert not Diagnoser.status()["enabled"]
    monkeypatch.setenv("REPOWATCH_DIAGNOSIS_ENABLED", "1")
    monkeypatch.setenv("REPOWATCH_DIAGNOSIS_MODEL", "configured-model")
    monkeypatch.setenv("REPOWATCH_DIAGNOSIS_API_KEY", "dedicated-secret-key")
    assert Diagnoser.status()["enabled"]
    assert "dedicated-secret-key" not in json.dumps(Diagnoser.status())
    for url in ("http://external.example/v1", "https://user:pass@example.com/v1",
                "https://example.com/v1?token=secret", "http://[", "https://example.com:bad/v1"):
        monkeypatch.setenv("REPOWATCH_DIAGNOSIS_BASE_URL", url)
        assert not Diagnoser.status()["enabled"]
    monkeypatch.setenv("REPOWATCH_DIAGNOSIS_BASE_URL", "http://127.0.0.1:1234/v1")
    assert Diagnoser.status()["enabled"]


class DiagnosticModel(Model):
    def __init__(self, *, skip_handoffs=False, invented_evidence=False):
        self.calls = 0
        self.skip_handoffs = skip_handoffs
        self.invented_evidence = invented_evidence

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        call_id = "diagnostic_" + str(self.calls)
        assert not kwargs["tools"]
        if kwargs["handoffs"] and not self.skip_handoffs:
            output = [ResponseFunctionToolCall(
                id=call_id, call_id=call_id, type="function_call",
                name=kwargs["handoffs"][0].tool_name, arguments="{}",
            )]
        else:
            advice = {
                "summary": "Check the build configuration.",
                "evidence": ["invented error" if self.invented_evidence else UNKNOWN_LOG],
                "steps": ["Check the project manifest."], "confidence": "uncertain",
                "patch": {"install": None, "test": None, "start": None,
                          "port": None, "install_network": None},
            }
            output = [ResponseOutputMessage(
                id=call_id, type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=json.dumps(advice),
                                            annotations=[])],
            )]
        return ModelResponse(output=output, usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError()
        yield


def test_actual_sdk_handoffs_and_reviewed_structured_output():
    model = DiagnosticModel()
    report = asyncio.run(run_agents(model, {
        "log": UNKNOWN_LOG, "manifests": {}, "config": {},
    }))
    assert model.calls == 3
    assert report["source"] == "model"
    assert report["handoffs"] == [
        "repository_analyst -> reproduction_assistant",
        "reproduction_assistant -> independent_reviewer",
    ]
    root = agents_for(model)
    assert root.tools == [] and root.handoffs[0].tools == []
    assert root.handoffs[0].handoffs[0].tools == []


@pytest.mark.parametrize("flags,error", [
    ({"skip_handoffs": True}, "diagnosis_handoff_incomplete"),
    ({"invented_evidence": True}, "diagnosis_evidence_unverified"),
])
def test_model_cannot_skip_review_or_invent_evidence(flags, error):
    with pytest.raises(ValueError, match=error):
        asyncio.run(run_agents(DiagnosticModel(**flags), {"log": UNKNOWN_LOG, "manifests": {}}))


def test_background_model_failure_preserves_local_report(service):  # noqa: F811
    execution, _, _ = service

    class Unavailable:
        def status(self):
            return {"enabled": True}

        async def analyze(self, context):
            raise RuntimeError("provider secret must not be persisted")

    execution.diagnoses.engine = Unavailable()
    env = failure(service, UNKNOWN_LOG)
    assert env["diagnosis"]["state"] == "queued"
    assert not env["busy"]
    assert asyncio.run(execution.diagnoses.tick())
    result = execution.get(env["id"])["diagnosis"]
    assert result["state"] == "failed" and result["report"]["source"] == "local_rules"
    assert "provider secret" not in json.dumps(result)
    assert result["report"]["model_status"] == "unavailable"


def test_background_sdk_diagnosis_persists_handoffs_without_running_project(service):  # noqa: F811
    execution, _, runtime = service

    class Engine:
        def status(self):
            return {"enabled": True}

        async def analyze(self, context):
            return await run_agents(DiagnosticModel(), context)

    execution.diagnoses.engine = Engine()
    env = failure(service, UNKNOWN_LOG)
    assert asyncio.run(execution.diagnoses.tick())
    result = execution.get(env["id"])
    assert result["diagnosis"]["report"]["source"] == "model"
    assert len(result["diagnosis"]["report"]["handoffs"]) == 2
    assert not result["installed"] and result["state"] == "setup_failed"
    assert runtime.calls == []


def test_interrupted_diagnosis_recovers_without_replaying_model(service):  # noqa: F811
    execution, _, _ = service
    env = failure(service, UNKNOWN_LOG)
    with execution.store.connection() as db:
        db.execute("UPDATE execution_diagnoses SET state='running',lease=?", (time.time() - 1,))
    asyncio.run(execution.diagnoses.tick())
    result = execution.get(env["id"])["diagnosis"]
    assert result["state"] == "failed" and result["report"]["model_status"] == "interrupted"


def test_apply_api_requires_csrf_version_and_server_saved_patch(tmp_path):
    remote, vault, runtime = Remote(), MemoryVault(), Runtime()
    runtime.run = lambda *_: (1, DNS_LOG)
    app = create_app(tmp_path / "api.db", start_worker=False, github_client=remote.client,
                     credential_vault=vault, execution_runtime=runtime, repository_source=Source())
    with TestClient(app, base_url="http://localhost") as client:
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        client.post("/api/github/accounts", headers=headers,
                    json={"username": "alice", "token": TOKEN_A})
        repo = client.post("/api/repositories", headers=headers,
                           json={"url": "https://github.com/alice/project"}).json()
        env = client.post(f"/api/repositories/{repo['id']}/environments",
                          headers=headers, json={}).json()
        execution = app.state.execution
        execution.tick()
        env = execution.get(env["id"])
        execution.action(env["id"], env["version"], "setup")
        execution.tick()
        env = execution.get(env["id"])
        url = f"/api/environments/{env['id']}/diagnoses/{env['diagnosis']['job_id']}/apply"
        assert client.post(url, json={"version": env["version"]}).status_code == 403
        assert client.post(url, headers=headers, json={
            "version": env["version"], "patch": {"install": "malicious"},
        }).status_code == 422
        result = client.post(url, headers=headers, json={"version": env["version"]})
        assert result.status_code == 202 and result.json()["config"]["install_network"]
        assert client.post(url, headers=headers,
                           json={"version": env["version"]}).status_code == 409
