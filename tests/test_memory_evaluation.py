import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import Runtime, Source
from test_github_accounts import TOKEN_A, MemoryVault, Remote
from test_repository_analysis import prepare_tested

from repowatch.app import create_app
from repowatch.memory import Memory
from repowatch.repository_analysis import ROLES
from repowatch.store import Conflict


class EvaluationEngine:
    enabled = True

    def __init__(self):
        self.memory_counts = []

    def status(self):
        return {"enabled": True, "model": "test-only"}

    async def analyze(self, context, event):
        event("model_call", "test-only")
        self.memory_counts.append(len(context.get("memories", [])))
        findings = []
        if context.get("memories"):
            findings.append({
                "title": "Memory-assisted finding", "severity": "P2",
                "explanation": "Memory-assisted fixture finding.",
                "expected": "Expected behavior", "suspected_actual": "Actual behavior",
                "reproduction": ["Operate the fixture."],
                "evidence": [{"path": "app.py", "start_line": 1, "end_line": 1,
                              "quote": "print('ok')"}],
            })
        return {
            "summary": "Deterministic evaluation fixture.", "limitations": [],
            "findings": findings,
            "handoffs": [f"{a} -> {b}" for a, b in zip(ROLES, ROLES[1:])],
            "files_read": ["app.py"], "execution_read": True,
            "read_ranges": {"app.py": [[1, 1]]},
        }


def service(tmp_path, engine):
    app = create_app(
        Path(tmp_path) / "evaluation.db", start_worker=False,
        github_client=Remote().client, credential_vault=MemoryVault(),
        execution_runtime=Runtime(), repository_source=Source(),
        analysis_engine=engine,
    )
    return app


def prepared(app):
    execution = app.state.execution
    execution.github.connect("alice", TOKEN_A)
    repo = execution.store.add_repository(
        "alice/project", "https://github.com/alice/project"
    )
    env = prepare_tested((execution, repo, execution.runtime))
    return execution, repo, env


def test_memory_evaluation_runs_paired_variants(tmp_path):
    engine = EvaluationEngine()
    app = service(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        execution, repo, env = prepared(app)
        Memory(app.state.store).create(
            repo["id"], category="testing", title="Relevant memory",
            content="Review app.py for the known fixture behavior.",
            revision=env["revision"],
        )
        analyses = app.state.analyses
        analyses.enqueue(env["id"], env["version"], manual=True)
        asyncio.run(analyses.tick())
        analysis = analyses.list(env["id"])[0]
        assert analysis["status"] == "completed"
        engine.memory_counts.clear()

        created = client.post(
            f"/api/analyses/{analysis['id']}/memory-evaluation", headers=headers
        )
        assert created.status_code == 202
        evaluation_id = created.json()["id"]
        again = client.post(
            f"/api/analyses/{analysis['id']}/memory-evaluation", headers=headers
        ).json()
        assert again["id"] == evaluation_id

        assert asyncio.run(app.state.memory_evaluations.tick()) is True
        detail = client.get(
            f"/api/analyses/{analysis['id']}/memory-evaluation", headers=headers
        ).json()
        assert detail["status"] == "completed"
        assert engine.memory_counts == [0, 1]
        assert detail["summary"]["finding_counts"] == {"baseline": 0, "memory": 1}
        assert detail["summary"]["finding_only_memory"]
        assert detail["summary"]["memory_entry_count"] == 1
        assert detail["summary"]["paths"]["only_memory"] == ["app.py"]


def test_memory_evaluation_requires_memory_and_owner(tmp_path):
    engine = EvaluationEngine()
    app = service(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        execution, _, env = prepared(app)
        analyses = app.state.analyses
        analyses.enqueue(env["id"], env["version"], manual=True)
        asyncio.run(analyses.tick())
        analysis = analyses.list(env["id"])[0]
        url = f"/api/analyses/{analysis['id']}/memory-evaluation"
        assert client.post(url).status_code == 401
        client.post("/api/setup", json={"password": "owner-test-password"})
        assert client.post(url).status_code == 403
        with pytest.raises(Conflict, match="memory_evaluation_memory_required"):
            app.state.memory_evaluations.enqueue(analysis["id"])
