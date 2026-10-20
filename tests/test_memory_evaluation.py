import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import Runtime, Source
from test_github_accounts import TOKEN_A, MemoryVault, Remote
from test_repository_analysis import prepare_tested

from repowatch.app import create_app
from repowatch.memory import Memory
from repowatch.memory_evaluation import initialize
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


class FailingEvaluationEngine(EvaluationEngine):
    fail_memory = False

    async def analyze(self, context, event):
        if self.fail_memory and context.get("memories"):
            event("model_call", "test-only")
            raise ValueError("analysis_evidence_unverified")
        return await super().analyze(context, event)


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


def test_memory_evaluation_retains_baseline_when_memory_variant_fails(tmp_path):
    engine = FailingEvaluationEngine()
    app = service(tmp_path, engine)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        session = client.post("/api/setup", json={"password": "owner-test-password"}).json()
        headers = {"x-csrf-token": session["csrf"]}
        execution, repo, env = prepared(app)
        Memory(app.state.store).create(
            repo["id"], category="testing", title="Relevant memory",
            content="Review app.py.", revision=env["revision"],
        )
        analyses = app.state.analyses
        analyses.enqueue(env["id"], env["version"], manual=True)
        asyncio.run(analyses.tick())
        analysis = analyses.list(env["id"])[0]
        assert analysis["status"] == "completed"
        engine.fail_memory = True
        app.state.memory_evaluations.enqueue(analysis["id"])
        asyncio.run(app.state.memory_evaluations.tick())
        detail = client.get(
            f"/api/analyses/{analysis['id']}/memory-evaluation", headers=headers
        ).json()
        assert detail["status"] == "failed"
        assert detail["baseline"]["status"] == "completed"
        assert detail["memory"]["status"] == "failed"
        assert detail["memory_error"] == "analysis_evidence_unverified"
        assert detail["memory"]["model_calls"] == 1
        assert detail["summary"]["finding_counts"]["baseline"] == 0
        assert detail["summary"]["conclusion"] == "insufficient_evidence"
    reopened = service(tmp_path, engine)
    persisted = reopened.state.memory_evaluations.get_by_analysis(analysis["id"])
    assert persisted["baseline"] == detail["baseline"]
    assert persisted["memory_error"] == "analysis_evidence_unverified"


def test_memory_evaluation_recovers_interrupted_partial_result(tmp_path):
    engine = EvaluationEngine()
    app = service(tmp_path, engine)
    execution, repo, env = prepared(app)
    app.state.analyses.enqueue(env["id"], env["version"], manual=True)
    asyncio.run(app.state.analyses.tick())
    analysis = app.state.analyses.list(env["id"])[0]
    with app.state.store.connection() as db:
        db.execute(
            "INSERT INTO memory_evaluations(id,analysis_id,repo_id,revision,status,"
            "baseline,created,updated) VALUES(?,?,?,?,?,?,?,?)",
            ("eval_test", analysis["id"], repo["id"], env["revision"], "running",
             '{"status":"completed","report":{"findings":[]},"model_calls":1,'
             '"elapsed_seconds":2,"memories":[]}', 0, time.time()),
        )
    reopened = service(tmp_path, engine)
    assert reopened.state.memory_evaluations.get("eval_test")["status"] == "running"
    with reopened.state.store.connection() as db:
        db.execute("UPDATE memory_evaluations SET updated=0 WHERE id='eval_test'")
    asyncio.run(reopened.state.memory_evaluations.tick())
    row = reopened.state.memory_evaluations.get("eval_test")
    assert row["status"] == "failed"
    assert row["error"] == "memory_evaluation_interrupted"
    assert row["baseline"]["model_calls"] == 1
    assert row["summary"]["finding_counts"]["baseline"] == 0


def test_memory_evaluation_adds_failure_columns_to_old_table(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE memory_evaluations (id TEXT PRIMARY KEY, analysis_id TEXT,"
            "repo_id TEXT, revision TEXT, status TEXT, baseline TEXT, memory TEXT,"
            "summary TEXT, error TEXT, version INTEGER, created REAL, updated REAL)"
        )
        initialize(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(memory_evaluations)")}
    assert {"baseline_error", "memory_error"} <= columns
