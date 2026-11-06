import json
from types import SimpleNamespace

import pytest
from test_roles import kernel as kernel

from repowatch.application.candidate_issues import CandidateIssues
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.domain.triage import TriageAnalysis
from repowatch.worker.triage import TriageCoordinator


@pytest.mark.parametrize(
    "scenario", ["create", "existing", "lost", "sensitive", "uncertain", "raced"]
)
def test_candidate_issue_identity_budget_and_unknown_receipt(kernel, monkeypatch, scenario):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace(read=lambda reference: None)
    repository_id = services.tasks.register_repository("100", admission_mode="all_bugs")
    candidate_id = services.tasks.ensure_task(repository_id, "candidate:1", now=0)
    with services.store.write() as records:
        records.change(
            "tasks",
            {"id": candidate_id},
            {"configuration_json": canonical_json({"workspace_image": "fixture:image"})},
        )
        records.change(
            "repositories",
            {"id": repository_id},
            {
                "github_owner": "fixture",
                "github_name": "repo",
                "identity_json": canonical_json({"execution_login": "worker"}),
            },
        )
    services.budgets.authorize_initial(
        "budget",
        candidate_id,
        BudgetAmounts(
            active_ms=2000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    issues, writes = [], []

    class Port:
        def __init__(self, **kwargs):
            pass

        def _path(self, owner, name):
            return "/repos/fixture/repo"

        def collection(self, path, **kwargs):
            return list(issues)

        def write(self, method, path, body):
            writes.append(body)
            issue = {
                "id": 101,
                "number": 1,
                "state": "open",
                "title": body["title"],
                "body": body["body"],
                "user": {"login": "worker"},
            }
            issues.append(issue)
            if scenario == "lost":
                raise PermissionError("receipt lost")
            return issue

        def issue(self, owner, name, number):
            return next(item for item in issues if item["number"] == number)

    monkeypatch.setattr("repowatch.application.candidate_issues.GitHubPublisher", Port)
    if scenario == "existing":
        issues.append({"id": 101, "number": 1, "state": "open", "title": "existing"})
    analysis = TriageAnalysis(
        kind="bug",
        objective="sum defect",
        criteria=["sum is correct"],
        reproduction_steps=["call sum"],
        sensitive=scenario == "sensitive",
    )
    relation = {
        "relation": "same" if scenario == "existing" else "none",
        "issue_number": 1,
        "evidence": "full collection reviewed",
    }
    resolver = CandidateIssues(services)
    lookup = resolver.read(token)
    if scenario == "raced":
        issues.append({"id": 201, "number": 2, "title": "new issue", "state": "open"})
        with pytest.raises(ConflictError, match="重新查重"):
            resolver.resolve(token, analysis, lookup, relation)
        assert writes == []
        return
    if scenario == "sensitive":
        with pytest.raises(PermissionError, match="敏感"):
            resolver.resolve(token, analysis, lookup, relation)
        assert writes == []
        return
    if scenario == "uncertain":
        assert resolver.resolve(token, analysis, lookup, {"relation": "uncertain"}) is None
        assert writes == []
        return
    if scenario == "lost":
        with pytest.raises(PermissionError, match="receipt lost"):
            resolver.resolve(token, analysis, lookup, relation)
        with services.store.read() as records:
            intent = records.one("publication_intents", task_id=candidate_id, kind="issue")
            assert intent["state"] == "unknown"
        services.queue.complete(token, now=0, state="waiting", wait_reason="receipt")
        with services.store.read() as records:
            revision = records.one("tasks", id=candidate_id)["revision"]
        services.tasks.operate(
            "reconcile",
            candidate_id,
            expected_revision=revision,
            action="reconcile_issue",
            now=0,
        )
        token = services.queue.claim(now=0)
        result = TriageCoordinator(services, SimpleNamespace()).run(token)
        assert result["reason"] == "issue_confirmed"
        target_id = result["task_id"]
    else:
        target_id = resolver.resolve(token, analysis, lookup, relation)
    assert target_id != candidate_id
    assert len(writes) == (0 if scenario == "existing" else 1)
    with services.store.read() as records:
        target = records.one("tasks", id=target_id)
        assert target["issue_id"] == "101"
        assert target["budget_owner_task_id"] == candidate_id
        assert target["selected"] == 1
        assert json.loads(target["configuration_json"])["workspace_image"] == "fixture:image"
        assert len(records.find("tasks", repository_id=repository_id, issue_id="101")) == 1
        intent = records.one("publication_intents", task_id=candidate_id, kind="issue")
        if intent:
            assert intent["state"] == "confirmed"
            assert json.loads(intent["result_json"])["task_id"] == target_id
