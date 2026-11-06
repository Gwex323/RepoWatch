import json
from types import SimpleNamespace

import httpx
import pytest
from test_roles import bind, diagnose
from test_roles import kernel as kernel

from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime
from repowatch.worker.triage import TriageCoordinator


@pytest.mark.parametrize(
    "scenario",
    ["bug", "sensitive", "candidate_sensitive", "feature", "missing", "multi_goal", "converged"],
)
def test_formal_triage_uses_actual_sdk_and_original_budget(kernel, monkeypatch, scenario):
    services = kernel
    bind(services)
    diagnose(services, "processor")
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace(read=lambda reference: None)
    repo = services.tasks.register_repository("formal-triage", admission_mode="all_bugs")
    task = services.tasks.ensure_task(
        repo, "issue:1", issue_id=None if scenario == "candidate_sensitive" else "issue-1", now=0
    )
    services.tasks.observe(
        task,
        "issue:1",
        "v1",
        {
            "state": "open",
            "title": "sum fails",
            "body": "sum(2,3) returns -1",
            **({"candidate_id": "candidate"} if scenario == "candidate_sensitive" else {}),
        },
        now=0,
    )
    if scenario == "converged":
        services.tasks.operate(
            "converge",
            task,
            expected_revision=2,
            action="set_acceptance",
            payload={
                "kind": "bug",
                "content": {
                    "objective": "return correct sum",
                    "criteria": ["sum(2,3) == 5"],
                },
            },
            now=0,
        )
        services.tasks.operate(
            "approve", task, expected_revision=3, action="approve_acceptance", now=0
        )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repo},
            {
                "identity_json": canonical_json({"execution_login": "worker"}),
                "target_sha": "a" * 40,
                "github_owner": "fixture",
                "github_name": "source",
                "target_branch": "main",
                "credential_reference": "fixture",
            },
        )
    services.budgets.authorize_initial(
        "triage-budget",
        task,
        BudgetAmounts(
            active_ms=1000000,
            model_calls=10,
            tokens=1000000,
            repair_rounds=3,
            recovery_rounds=3,
        ),
        expected_revision=1,
        reason="受控分诊验收",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    calls = []

    def wire(request):
        body = json.loads(request.content)
        calls.append(body)
        envelope = next(
            json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith('{"protocol_version"')
        )
        results = [message for message in body["messages"] if message["role"] == "tool"]
        if not results:
            name, arguments = "read_evidence", {}
        elif len(results) == 1:
            name, arguments = (
                "write_analysis",
                {
                    "analysis": {
                        "kind": "feature" if scenario == "feature" else "bug",
                        "objective": "return correct sum",
                        "criteria": ["sum(2,3) == 5"],
                        "reproduction_steps": ["call sum(2,3)"],
                        "sensitive": scenario in {"sensitive", "candidate_sensitive"},
                        "plan": "add addition capability" if scenario == "feature" else "",
                        "missing_information": ["environment"] if scenario == "missing" else [],
                        "independent_goals": ["sum", "product"] if scenario == "multi_goal" else [],
                    }
                },
            )
        else:
            name, arguments = (
                "StructuredResult",
                {
                    "context": envelope["context"],
                    "mode": envelope["mode"],
                    "status": "completed",
                    "summary": "分诊完成",
                    "artifacts": [],
                    "unresolved": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": str(len(calls)),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    class Objects:
        def __init__(self, root):
            self.root = root

        def fetch(self, *arguments, **keywords):
            return "a" * 40

        def repository(self, identifier):
            return self.root / identifier

        def run(self, *arguments):
            return ("b" * 40).encode()

        def entries(self, *arguments):
            return [{"path": "sum.py", "oid": "c" * 40, "mode": "100644"}]

    monkeypatch.setattr("repowatch.worker.triage.GitObjects", Objects)
    monkeypatch.setattr(
        "repowatch.worker.triage.CandidateIssues.read",
        lambda self, token: {
            "digest": "fixture",
            "facts": {"issues": []},
            "artifact_id": "fixture",
        },
    )
    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
    result = TriageCoordinator(services, FormalRuntime(services)).run(token)
    assert result["state"] == ("queued" if scenario in {"bug", "converged"} else "waiting")
    assert len(calls) == 3
    assert services.budgets.view(task)["used"]["model_calls"] == 3
    with services.store.read() as records:
        decision = records.find("triage_decisions", task_id=task)[0]
        assert (
            decision["result"]
            == {
                "bug": "auto_repair",
                "feature": "needs_approval",
                "sensitive": "needs_information",
                "candidate_sensitive": "needs_information",
                "missing": "needs_information",
                "multi_goal": "needs_approval",
                "converged": "auto_repair",
            }[scenario]
        )
        assert json.loads(decision["evidence_json"])["artifact_id"]
        assert records.one("global_control", singleton=1)["paused"] == 0
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
        if scenario in {"bug", "feature", "multi_goal", "converged"}:
            spec = records.one("task_acceptance_specs", task_id=task, revision=2)
            assert spec["kind"] == ("feature" if scenario == "feature" else "bug")
            assert (spec["approved_revision"] is not None) == (scenario == "converged")
        else:
            assert not records.find("task_acceptance_specs", task_id=task)
