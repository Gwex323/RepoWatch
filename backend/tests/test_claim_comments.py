import json
from types import SimpleNamespace

import pytest
from test_roles import kernel as kernel

from repowatch.application.acceptance import acceptance_view
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.github_reads import issue_input
from repowatch.application.issue_context import IssueContexts
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.worker.repairs import RepairCoordinator


@pytest.mark.parametrize("scenario", ["disabled", "confirmed", "lost"])
def test_claim_comment_is_opt_in_and_unknown_receipt_is_not_replayed(kernel, monkeypatch, scenario):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace()
    repository_id = services.tasks.register_repository("claim-fixture", admission_mode="all_bugs")
    task_id = services.tasks.ensure_task(repository_id, "issue:1", issue_id="101", now=0)
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository_id},
            {
                "identity_json": canonical_json({"execution_login": "worker"}),
                "policy_json": canonical_json(
                    {
                        "claim_comment": scenario != "disabled",
                        "claim_template": "Working on issue",
                    }
                ),
            },
        )
        records.change("tasks", {"id": task_id}, {"input_json": '{"number":1}'})
    services.budgets.authorize_initial(
        "budget",
        task_id,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    writes = []

    class Port:
        def __init__(self, **kwargs):
            pass

        def _path(self, *args):
            return "/repos/fixture/repo"

        def write(self, method, path, body):
            writes.append({"id": 501, **body, "user": {"login": "worker"}})
            if scenario == "lost":
                raise PermissionError("receipt lost")
            return writes[0]

    monkeypatch.setattr("repowatch.application.publication.GitHubPublisher", Port)
    coordinator = RepairCoordinator.__new__(RepairCoordinator)
    coordinator.services = services
    coordinator.executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)
    with services.store.read() as records:
        task = dict(records.one("tasks", id=task_id))
        repository = dict(records.one("repositories", id=repository_id))
    context = {"facts": {"comments": []}}
    if scenario == "lost":
        with pytest.raises(PermissionError, match="receipt lost"):
            coordinator.claim(token, task, repository, context)
        with pytest.raises(PermissionError, match="回执未知"):
            coordinator.claim(token, task, repository, context)
    else:
        coordinator.claim(token, task, repository, context)
    if scenario == "disabled":
        assert writes == []
        with services.store.read() as records:
            assert not records.find("publication_intents", task_id=task_id)
        return
    context["facts"]["comments"] = list(writes)
    coordinator.claim(token, task, repository, context)
    assert len(writes) == 1
    with services.store.read() as records:
        intent = records.one("publication_intents", task_id=task_id, kind="claim")
        assert intent["state"] == "confirmed"
        assert json.loads(intent["result_json"])["id"] == 501


def test_own_claim_comment_does_not_invalidate_approved_acceptance(kernel, monkeypatch):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace()
    repository_id = services.tasks.register_repository(
        "claim-integration", admission_mode="all_bugs"
    )
    task_id = services.tasks.ensure_task(repository_id, "issue:1", issue_id="101", now=0)
    issue = {
        "id": 101,
        "number": 1,
        "title": "add a status view",
        "body": "Please add a status view.",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    services.tasks.observe(task_id, "issue:1", "issue-v1", issue_input(issue), now=0)
    payload = {
        "kind": "feature",
        "content": {
            "objective": "show status",
            "plan": "add a status view",
            "criteria": ["the status is visible"],
        },
    }
    with services.store.read() as records:
        revision = records.one("tasks", id=task_id)["revision"]
    services.tasks.operate(
        "claim-feature-spec",
        task_id,
        expected_revision=revision,
        action="set_acceptance",
        payload=payload,
        now=0,
    )
    with services.store.read() as records:
        revision = records.one("tasks", id=task_id)["revision"]
    services.tasks.operate(
        "claim-feature-approve",
        task_id,
        expected_revision=revision,
        action="approve_acceptance",
        now=0,
    )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository_id},
            {
                "github_owner": "fixture",
                "github_name": "repo",
                "target_sha": "a" * 40,
                "scan_ready_sha": "a" * 40,
                "policy_json": canonical_json(
                    {"claim_comment": True, "claim_template": "Working on issue"}
                ),
            },
        )
        records.change(
            "tasks",
            {"id": task_id},
            {"mode": "implement", "input_json": canonical_json(issue_input(issue))},
        )
    services.budgets.authorize_initial(
        "claim-feature-budget",
        task_id,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    assert token is not None
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository_id},
            {"identity_json": canonical_json({"execution_login": "worker"})},
        )
    comments = []
    facts = {"issue": issue, "comments": comments, "pull_requests": [], "complete": True}

    class ReadPort:
        def __init__(self, **kwargs):
            pass

        def issue_context(self, *args):
            return facts

    class WritePort:
        def __init__(self, **kwargs):
            pass

        def _path(self, *args):
            return "/repos/fixture/repo"

        def write(self, method, path, body):
            comment = {"id": 501, "body": body["body"], "user": {"login": "worker"}}
            comments.append(comment)
            return comment

    monkeypatch.setattr("repowatch.application.issue_context.GitHubReadAdapter", ReadPort)
    monkeypatch.setattr("repowatch.application.publication.GitHubPublisher", WritePort)
    context = IssueContexts(services).read(token)
    with services.store.read() as records:
        task = dict(records.one("tasks", id=task_id))
        repository = dict(records.one("repositories", id=repository_id))
        before = acceptance_view(records, task)
    coordinator = RepairCoordinator.__new__(RepairCoordinator)
    coordinator.services = services
    coordinator.executor = BudgetedExecutor(services.budgets, services.queue, clock=services.clock)
    coordinator.claim(token, task, repository, context)

    reread = IssueContexts(services).read(token)
    assert reread["digest"] == context["digest"]

    with services.store.read() as records:
        current = dict(records.one("tasks", id=task_id))
        after = acceptance_view(records, current)
        assert current["feedback_revision"] == task["feedback_revision"]
        assert after["approved"] is True
        assert after["revision"] == before["revision"]
        assert len(reread["facts"]["comments"]) == 1


@pytest.mark.parametrize("change", ["external", "forged", "edited", "retimestamped"])
def test_own_claim_comment_is_ignored_but_external_comment_still_invalidates(
    kernel, monkeypatch, change
):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace()
    repository_id = services.tasks.register_repository("claim-filter", admission_mode="all_bugs")
    task_id = services.tasks.ensure_task(repository_id, "issue:2", issue_id="102", now=0)
    issue = {"id": 102, "number": 2, "state": "open", "title": "bug"}
    services.tasks.observe(task_id, "issue:2", "issue-v1", issue_input(issue), now=0)
    services.budgets.authorize_initial(
        "claim-filter-budget",
        task_id,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository_id},
            {
                "github_owner": "fixture",
                "github_name": "repo",
                "identity_json": canonical_json({"execution_login": "worker"}),
            },
        )
    comments = []
    facts = {"issue": issue, "comments": comments, "pull_requests": [], "complete": True}

    class Port:
        def __init__(self, **kwargs):
            pass

        def issue_context(self, *args):
            return facts

    monkeypatch.setattr("repowatch.application.issue_context.GitHubReadAdapter", Port)
    reader = IssueContexts(services)
    reader.read(token)
    marker = "<!-- repowatch-claim:" + task_id + " -->"
    body = "claim\n\n" + marker
    with services.store.write() as records:
        records.add(
            "publication_intents",
            {
                "id": "claim-intent",
                "task_id": task_id,
                "repository_id": repository_id,
                "issue_id": "102",
                "kind": "claim",
                "request_json": canonical_json({"body": body}),
                "request_sha256": "fixture",
                "result_json": canonical_json({"id": 501}),
                "state": "confirmed",
                "created_at": 0,
            },
        )
    comments.append({"id": 501, "body": body, "user": {"login": "worker"}})
    reader.read(token)
    if change == "external":
        comments.append({"id": 502, "body": "external guidance", "user": {"login": "maintainer"}})
    elif change == "forged":
        comments.append({"id": 502, "body": body, "user": {"login": "maintainer"}})
    elif change == "edited":
        comments[0]["body"] += "\nNew instructions"
    else:
        comments[0].update(created_at="2026-11-06T00:00:00Z", updated_at="2026-11-06T01:00:00Z")
    with pytest.raises(ConflictError, match="相关评论或 PR"):
        reader.read(token)
    with services.store.read() as records:
        assert records.one("tasks", id=task_id)["feedback_revision"] == 2
        assert len(records.find("issue_contexts", task_id=task_id)) == 2
