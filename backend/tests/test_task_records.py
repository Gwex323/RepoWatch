import json

import pytest

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.orchestration import Orchestrator
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import TriageDecision, TriageResult
from repowatch.operations.migrate import migrate


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks = TaskRecords(store)
    queue = Orchestrator(store)
    repo = tasks.register_repository("stable-github-repo", admission_mode="all_bugs")
    queue.set_pause(False, now=0)
    yield store, tasks, queue, repo
    store.close()


def row(store, collection, **identity):
    with store.read() as records:
        return dict(records.one(collection, **identity))


def task_for(kernel, *, issue_id="issue-1", source="issue:1"):
    _, tasks, _, repo = kernel
    return tasks.ensure_task(repo, source, issue_id=issue_id, now=0)


def operate(kernel, task, action, **kwargs):
    store, service, _, _ = kernel
    return service.operate(
        kwargs.pop("operation_id", "operation-1"),
        task,
        expected_revision=kwargs.pop("expected_revision", row(store, "tasks", id=task)["revision"]),
        action=action,
        now=5,
        **kwargs,
    )


def test_stable_repository_issue_and_source_aliases_do_not_copy_tasks(kernel):
    store, tasks, _, repo = kernel
    task = task_for(kernel)
    assert tasks.register_repository("stable-github-repo", admission_mode="manual") == repo
    assert tasks.ensure_task(repo, "feedback:1", issue_id="issue-1", now=2) == task
    with pytest.raises(ConflictError):
        tasks.ensure_task(repo, "feedback:1", issue_id="issue-2", now=2)
    assert tasks.ensure_task(repo, "issue:1", issue_id="issue-1", now=3) == task
    with store.read() as records:
        assert len(records.find("tasks")) == 1
        assert len(records.find("task_sources")) == 2
        assert len(records.find("budget_ledgers")) == 1


def test_signal_dedup_and_revision_conflict_are_atomic(kernel):
    store, tasks, _, _ = kernel
    task = task_for(kernel)
    signal = tasks.observe(task, "review:1", "v1", {"body": "first"}, kind="feedback", now=1)
    assert (
        tasks.observe(task, "review:1", "v1", {"body": "first"}, kind="feedback", now=1) == signal
    )
    with pytest.raises(ConflictError):
        tasks.observe(task, "review:1", "v1", {"body": "conflicting"}, kind="feedback", now=1)
    with store.read() as records:
        assert len(records.find("tasks")) == 1
        assert len(records.find("pending_signals")) == 1
        assert records.one("tasks", id=task)["feedback_revision"] == 2
        assert len(records.find("business_events", kind="signal_observed")) == 1


def test_run_snapshot_and_acknowledgement_preserve_new_feedback(kernel):
    store, tasks, queue, _ = kernel
    task = task_for(kernel)
    first = tasks.observe(task, "review:1", "v1", {"body": "first"}, kind="feedback", now=1)
    token = queue.claim(now=2)
    original = row(store, "runs", id=token.run_id)
    second = tasks.observe(task, "review:2", "v2", {"body": "second"}, kind="feedback", now=3)
    tasks.acknowledge(token, now=4)
    assert row(store, "runs", id=token.run_id) == original
    assert row(store, "pending_signals", id=first)["consumed_by_run_id"] == token.run_id
    assert row(store, "pending_signals", id=second)["consumed_by_run_id"] is None
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.change("runs", {"id": token.run_id}, {"snapshot_json": "{}"})


@pytest.mark.parametrize(
    ("classification", "expected_state", "wait_reason"),
    [
        ("auto_repair", "queued", None),
        ("needs_information", "waiting", "information"),
        ("needs_approval", "waiting", "approval"),
        ("excluded", "ended", None),
        ("external_repair", "waiting", "external_pr"),
        ("blocked", "blocked", None),
    ],
)
def test_six_triage_results_are_distinct_and_saved_before_release(
    kernel,
    classification,
    expected_state,
    wait_reason,
):
    store, tasks, queue, _ = kernel
    task = task_for(kernel)
    token = queue.claim(now=1)
    decision = TriageDecision(TriageResult(classification), "test_reason", {"issue": "revision:1"})
    result = tasks.save_triage(token, decision, now=2)
    assert tasks.save_triage(token, decision, now=2) == result
    assert row(store, "tasks", id=task)["state"] == "running"
    assert row(store, "execution_lease", singleton=1)["holder_run_id"] == token.run_id
    queue.complete(token, now=3)
    state = row(store, "tasks", id=task)
    assert (state["state"], state["wait_reason"]) == (expected_state, wait_reason)
    assert row(store, "global_control", singleton=1)["paused"] == (classification == "blocked")


def test_old_input_triage_does_not_override_current_state(kernel):
    store, tasks, queue, _ = kernel
    task = task_for(kernel)
    token = queue.claim(now=1)
    tasks.observe(task, "issue:1", "new", {"body": "new scope"}, now=2)
    tasks.save_triage(token, TriageDecision("auto_repair", "bug", {"issue": "old"}), now=3)
    queue.complete(token, now=4)
    state = row(store, "tasks", id=task)
    assert (state["state"], state["mode"]) == ("queued", "triage")
    with store.read() as records:
        assert len(records.find("triage_decisions")) == 1


def test_human_operations_replay_exactly_and_reject_changed_or_stale_request(kernel):
    store, service, _, _ = kernel
    task = task_for(kernel)
    revision = row(store, "tasks", id=task)["revision"]
    first = operate(kernel, task, "exclude", expected_revision=revision)
    assert operate(kernel, task, "exclude", expected_revision=revision) == first
    with pytest.raises(ConflictError):
        operate(kernel, task, "cancel", expected_revision=revision)
    with pytest.raises(ConflictError):
        operate(kernel, task, "select", operation_id="new-op", expected_revision=revision)
    restarted = SQLiteBusinessStore(store.settings)
    assert (
        TaskRecords(restarted).operate(
            "operation-1", task, expected_revision=revision, action="exclude", now=20
        )
        == first
    )
    restarted.close()
    with store.read() as records:
        assert len(records.find("user_operations")) == 1
        assert len(records.find("business_events", kind="user_operation")) == 1


@pytest.mark.parametrize("action", ["exclude", "cancel"])
def test_user_exclusion_survives_sync_reimport_and_requires_explicit_reenable(kernel, action):
    store, tasks, _, repo = kernel
    task = task_for(kernel)
    operate(kernel, task, action)
    tasks.observe(task, "issue:1", "reopened", {"state": "open"}, now=6)
    assert tasks.register_repository("stable-github-repo", admission_mode="all_bugs") == repo
    assert tasks.ensure_task(repo, "reimport:1", issue_id="issue-1", now=7) == task
    assert row(store, "tasks", id=task)["user_excluded"] == 1
    with pytest.raises(ConflictError):
        operate(kernel, task, "select", operation_id="ordinary-select")
    result = operate(kernel, task, "reenable", operation_id="explicit-reenable")
    assert result["state"] == "queued"
    assert row(store, "tasks", id=task)["user_excluded"] == 0


def test_policy_exclusion_can_be_reconsidered_but_manual_mode_still_needs_selection(kernel):
    store, tasks, queue, repo = kernel
    task = task_for(kernel)
    token = queue.claim(now=1)
    tasks.save_triage(
        token, TriageDecision("excluded", "other_assignee", {"assignee": "other"}), now=2
    )
    queue.complete(token, now=3)
    tasks.observe(task, "issue:1", "changed-assignee", {"assignee": None}, now=4)
    assert row(store, "tasks", id=task)["state"] == "queued"
    manual_repo = tasks.register_repository("manual-repo", admission_mode="manual")
    manual_task = tasks.ensure_task(manual_repo, "issue:m", issue_id="m", now=5)
    tasks.observe(manual_task, "issue:m", "v2", {"labels": ["bug"]}, now=6)
    assert row(store, "tasks", id=manual_task)["state"] == "waiting"


def test_closed_managed_pr_keeps_both_unique_identities_and_external_prs_are_observations(kernel):
    store, tasks, _, repo = kernel
    task = task_for(kernel)
    other = task_for(kernel, issue_id="issue-2", source="issue:2")
    tasks.bind_managed_pr(task, "fork-repo", "pr-1", state="closed")
    tasks.bind_managed_pr(task, "fork-repo", "pr-1", state="closed")
    with pytest.raises(ConflictError):
        tasks.bind_managed_pr(task, "fork-repo", "replacement")
    with pytest.raises(ConflictError):
        tasks.bind_managed_pr(other, "fork-repo", "pr-1")
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.remove("managed_pull_requests", task_id=task)
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.change("managed_pull_requests", {"task_id": task}, {"pr_id": "replacement"})
    for pr in ("external-1", "external-2"):
        tasks.observe_external_pr(repo, "issue-1", pr, {"state": "open"})
    with store.read() as records:
        assert len(records.find("external_pr_observations")) == 2
        assert len(records.find("managed_pull_requests")) == 1
    operate(kernel, task, "cancel")
    result = operate(kernel, task, "reenable", operation_id="reenable")
    assert result["state"] == "waiting"
    assert row(store, "tasks", id=task)["wait_reason"] == "delivery_review"


def test_signal_and_input_roll_back_if_event_cannot_be_persisted(kernel, monkeypatch):
    from repowatch.adapters.storage.sqlite import SqlRecords

    store, tasks, _, _ = kernel
    task = task_for(kernel)
    original = SqlRecords.add

    def fail_event(self, collection, values):
        if collection == "business_events":
            raise StorageError("模拟事件落盘失败")
        return original(self, collection, values)

    monkeypatch.setattr(SqlRecords, "add", fail_event)
    with pytest.raises(StorageError):
        tasks.observe(task, "issue:1", "v2", {"body": "changed"}, now=1)
    with store.read() as records:
        assert records.find("pending_signals") == []
        assert records.one("tasks", id=task)["input_revision"] == 1


def test_new_input_and_feedback_versions_are_separate(kernel):
    store, tasks, queue, _ = kernel
    task = task_for(kernel)
    tasks.observe(task, "issue:1", "v1", {"body": "issue"}, now=1)
    tasks.observe(task, "review:1", "v1", {"body": "feedback"}, kind="feedback", now=2)
    token = queue.claim(now=3)
    run = row(store, "runs", id=token.run_id)
    assert (run["input_revision"], run["feedback_revision"]) == (2, 2)
    assert json.loads(run["snapshot_json"])["input"] == {"body": "issue"}
