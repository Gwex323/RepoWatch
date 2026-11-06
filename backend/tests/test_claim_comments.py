import json
from types import SimpleNamespace

import pytest
from test_roles import kernel as kernel

from repowatch.application.budgets import BudgetedExecutor
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
