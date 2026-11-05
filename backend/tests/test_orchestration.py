import multiprocessing

import pytest

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.orchestration import Orchestrator
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json
from repowatch.operations.migrate import migrate


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks = TaskRecords(store)
    queue = Orchestrator(store, aging_interval=10)
    repo = tasks.register_repository("repo", admission_mode="all_bugs")
    queue.set_pause(False, now=0)
    yield settings, store, tasks, queue, repo
    store.close()


def read(store, collection, **identity):
    with store.read() as records:
        return dict(records.one(collection, **identity))


def task(kernel, name, *, mode="triage", now=0):
    _, _, tasks, _, repo = kernel
    return tasks.ensure_task(
        repo, name, issue_id=name if mode != "scan" else None, mode=mode, now=now
    )


def compete(settings, start, results):
    store = SQLiteBusinessStore(settings)
    try:
        if not start.wait(15):
            raise TimeoutError("竞争进程未就绪")
        results.put(Orchestrator(store).claim(now=1))
    finally:
        store.close()


def test_e7_multi_processes_claim_only_one_execution_slot(kernel):
    settings, _, _, _, _ = kernel
    task(kernel, "one")
    task(kernel, "two")
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    workers = [context.Process(target=compete, args=(settings, gate, results)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        gate.set()
        outcomes = [results.get(timeout=25) for _ in workers]
        assert sum(result is not None for result in outcomes) == 1
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        results.close()
        results.join_thread()


def test_waiting_or_unscanned_queue_head_does_not_block_ready_task(kernel):
    _, store, _, queue, _ = kernel
    waiting = task(kernel, "waiting", mode="implement")
    ready = task(kernel, "ready", now=1)
    queue.set_priority(waiting, manual=100, now=1)
    token = queue.claim(now=2)
    assert read(store, "runs", id=token.run_id)["task_id"] == ready
    assert read(store, "tasks", id=waiting)["ready_seconds"] == 0


def test_manual_priority_dominates_automatic_aging_and_does_not_preempt(kernel):
    _, store, _, queue, _ = kernel
    automatic = task(kernel, "old")
    manual = task(kernel, "manual", now=100)
    queue.set_priority(manual, manual=-100, now=100)
    token = queue.claim(now=101)
    assert read(store, "runs", id=token.run_id)["task_id"] == manual
    queue.set_priority(automatic, manual=100, now=102)
    assert queue.claim(now=103) is None
    queue.complete(token, now=104)
    next_token = queue.claim(now=105)
    assert next_token.generation == token.generation + 1
    assert read(store, "runs", id=next_token.run_id)["task_id"] == automatic


def test_automatic_aging_preserves_original_enqueue_time_and_excludes_pause(kernel):
    _, store, _, queue, _ = kernel
    old = task(kernel, "old")
    queue.set_pause(True, now=5)
    queue.set_pause(False, now=105)
    newer = task(kernel, "new", now=106)
    queue.set_priority(newer, severity=1, now=106)
    token = queue.claim(now=111)
    assert read(store, "runs", id=token.run_id)["task_id"] == old
    before = read(store, "tasks", id=old)
    assert before["ready_seconds"] == 11  # 5 before pause + 6 after pause
    queue.complete(token, now=112, state="queued")
    again = read(store, "tasks", id=old)
    assert again["queued_at"] == 0
    assert again["ready_seconds"] == before["ready_seconds"]


def test_heartbeat_uses_short_independent_transaction_and_expiration_never_takes_over(kernel):
    _, store, _, queue, _ = kernel
    task(kernel, "one")
    task(kernel, "two")
    token = queue.claim(now=1, ttl=5)
    queue.heartbeat(token, now=5, ttl=5)
    assert read(store, "execution_lease", singleton=1)["expires_at"] == 10
    with pytest.raises(PermissionError):
        queue.guard(token, now=10)
    assert queue.claim(now=11) is None
    assert read(store, "execution_lease", singleton=1)["holder_run_id"] == token.run_id
    assert read(store, "global_control", singleton=1)["paused"] == 1
    with pytest.raises(PermissionError):
        queue.heartbeat(token, now=11)


def test_paused_run_can_save_result_but_cannot_start_more_work_or_clear_live_command(kernel):
    _, store, _, queue, _ = kernel
    task(kernel, "one")
    token = queue.claim(now=1)
    with store.write() as records:
        records.add(
            "command_states",
            {
                "id": "command",
                "run_id": token.run_id,
                "generation": token.generation,
                "state": "running",
                "identity_json": "{}",
            },
        )
    queue.set_pause(True, now=2)
    with pytest.raises(PermissionError):
        queue.guard(token, now=3)
    assert queue.guard(token, now=3, purpose="result")["id"] == token.run_id
    with pytest.raises(PermissionError):
        queue.complete(token, now=3, state="waiting", wait_reason="safe_stop")
    assert read(store, "execution_lease", singleton=1)["holder_run_id"] == token.run_id


def test_old_generation_cannot_guard_save_or_release_after_new_claim(kernel):
    _, _, _, queue, _ = kernel
    task(kernel, "one")
    task(kernel, "two")
    old = queue.claim(now=1)
    queue.complete(old, now=2)
    new = queue.claim(now=3)
    assert new.generation > old.generation
    for operation in (
        lambda: queue.guard(old, now=4),
        lambda: queue.heartbeat(old, now=4),
        lambda: queue.complete(old, now=4),
    ):
        with pytest.raises(PermissionError):
            operation()


def test_scan_dependency_is_saved_with_safe_release_and_wakes_atomically(kernel):
    _, store, tasks, queue, repo = kernel
    parent = task(kernel, "parent", mode="implement")
    with store.write() as records:
        records.change(
            "repositories", {"id": repo}, {"target_sha": "a" * 40, "scan_ready_sha": "a" * 40}
        )
    token = queue.claim(now=1)
    with store.write() as records:
        records.change("repositories", {"id": repo}, {"target_sha": "b" * 40})
    scan_id = queue.wait_for_scan(token, now=2)
    assert read(store, "tasks", id=parent)["wait_reason"] == "scan"
    assert read(store, "execution_lease", singleton=1)["holder_run_id"] is None
    assert read(store, "tasks", id=scan_id)["budget_owner_task_id"] == parent
    sibling = task(kernel, "sibling", mode="implement", now=2)
    queue.add_scan_dependency(sibling, scan_id, now=2)
    scan_token = queue.claim(now=3)
    assert read(store, "runs", id=scan_token.run_id)["task_id"] == scan_id
    queue.complete_scan(scan_token, now=4, validated_sha="b" * 40)
    assert read(store, "tasks", id=parent)["state"] == "queued"
    assert read(store, "tasks", id=sibling)["state"] == "queued"
    assert read(store, "repositories", id=repo)["scan_ready_sha"] == "b" * 40
    with store.read() as records:
        assert records.find("scan_dependencies") == []
        assert len(records.find("budget_ledgers")) == 2  # scan reuses parent's ledger


def shared_scan(kernel):
    _, store, tasks, queue, repo = kernel
    owner = task(kernel, "owner", mode="implement")
    other = task(kernel, "other", mode="implement")
    with store.write() as records:
        records.change(
            "repositories", {"id": repo}, {"target_sha": "a" * 40, "scan_ready_sha": "a" * 40}
        )
    queue.set_priority(owner, manual=1, now=0)
    token = queue.claim(now=1)
    with store.write() as records:
        records.change("repositories", {"id": repo}, {"target_sha": "b" * 40})
    scan = queue.wait_for_scan(token, now=2)
    queue.add_scan_dependency(other, scan, now=2)
    return owner, other, scan


def test_cancelling_non_owner_waiter_does_not_cancel_shared_scan(kernel):
    _, store, tasks, queue, _ = kernel
    owner, other, scan = shared_scan(kernel)
    tasks.operate(
        "cancel-other",
        other,
        action="cancel",
        now=3,
        expected_revision=read(store, "tasks", id=other)["revision"],
    )
    assert read(store, "tasks", id=scan)["state"] == "queued"
    assert read(store, "budget_ledgers", owner_task_id=owner)["revoked"] == 0
    with store.read() as records:
        assert len(records.find("scan_dependencies", scan_task_id=scan)) == 1
    scan_token = queue.claim(now=4)
    queue.complete_scan(scan_token, now=5, validated_sha="b" * 40)
    assert read(store, "tasks", id=other)["state"] == "cancelled"
    assert read(store, "tasks", id=owner)["state"] == "queued"


def test_cancelling_owner_revokes_authorization_and_retains_other_dependencies(kernel):
    _, store, tasks, queue, _ = kernel
    owner, other, scan = shared_scan(kernel)
    scan_token = queue.claim(now=3)
    tasks.operate(
        "cancel-owner",
        owner,
        action="cancel",
        now=4,
        expected_revision=read(store, "tasks", id=owner)["revision"],
    )
    with pytest.raises(PermissionError):
        queue.guard(scan_token, now=5)
    with pytest.raises(PermissionError):
        queue.complete_scan(scan_token, now=5, validated_sha="b" * 40)
    queue.complete(scan_token, now=5)
    assert read(store, "tasks", id=scan)["state"] == "blocked"
    assert read(store, "tasks", id=scan)["budget_owner_task_id"] == owner
    with store.read() as records:
        assert len(records.find("scan_dependencies", waiting_task_id=other)) == 1
    assert queue.claim(now=6) is None
    assert read(store, "global_control", singleton=1)["paused"] == 1


def test_scan_completion_rolls_back_wakeup_when_result_event_fails(kernel, monkeypatch):
    from repowatch.adapters.storage.sqlite import SqlRecords

    _, store, _, queue, _ = kernel
    owner, other, scan = shared_scan(kernel)
    token = queue.claim(now=3)
    original = SqlRecords.add

    def fail(self, collection, values):
        if collection == "business_events" and values["kind"] == "scan_dependencies_satisfied":
            raise StorageError("模拟扫描完成事件落盘失败")
        return original(self, collection, values)

    monkeypatch.setattr(SqlRecords, "add", fail)
    with pytest.raises(StorageError):
        queue.complete_scan(token, now=4, validated_sha="b" * 40)
    assert read(store, "tasks", id=owner)["state"] == "waiting"
    assert read(store, "tasks", id=other)["state"] == "waiting"
    with store.read() as records:
        assert len(records.find("scan_dependencies", scan_task_id=scan)) == 2
        assert records.one("execution_lease", singleton=1)["holder_run_id"] == token.run_id


def test_scan_for_old_target_cannot_mark_new_target_ready(kernel):
    _, store, _, queue, repo = kernel
    shared_scan(kernel)
    token = queue.claim(now=3)
    with store.write() as records:
        records.change("repositories", {"id": repo}, {"target_sha": "c" * 40})
    with pytest.raises(ConflictError):
        queue.complete_scan(token, now=4, validated_sha="b" * 40)
    assert read(store, "repositories", id=repo)["scan_ready_sha"] == "a" * 40


def test_scan_dependency_does_not_reenable_cancelled_task_or_override_approval_wait(kernel):
    _, store, tasks, queue, _ = kernel
    _, other, scan = shared_scan(kernel)
    tasks.operate(
        "cancel-other",
        other,
        action="cancel",
        now=3,
        expected_revision=read(store, "tasks", id=other)["revision"],
    )
    with pytest.raises(ConflictError):
        queue.add_scan_dependency(other, scan, now=4)
    approval = task(kernel, "approval", now=4)
    with store.write() as records:
        records.change("tasks", {"id": approval}, {"state": "waiting", "wait_reason": "approval"})
    queue.add_scan_dependency(approval, scan, now=4)
    scan_token = queue.claim(now=5)
    queue.complete_scan(scan_token, now=6, validated_sha="b" * 40)
    assert read(store, "tasks", id=approval)["wait_reason"] == "approval"


def test_normal_waiting_does_not_block_queue_or_count_as_ready_time(kernel):
    _, store, _, queue, _ = kernel
    first = task(kernel, "first")
    token = queue.claim(now=1)
    queue.complete(token, now=2, state="waiting", wait_reason="review")
    second = task(kernel, "second", now=100)
    other = queue.claim(now=101)
    assert read(store, "runs", id=other.run_id)["task_id"] == second
    assert read(store, "tasks", id=first)["ready_seconds"] == 1
    assert canonical_json({"x": 1}) == '{"x":1}'
