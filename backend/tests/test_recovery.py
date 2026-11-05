import json
import os
import subprocess
import sys
import time

import pytest
from langgraph.graph import END, START, StateGraph

from repowatch.adapters.storage.checkpoints import checkpoint_store
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.budgets import BudgetService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.recovery import RecoveryService
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.operations.migrate import migrate


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repo = tasks.register_repository("repo", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, "issue:1", issue_id="issue", now=0)
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "initial",
        task,
        BudgetAmounts(
            active_ms=100000, model_calls=10, tokens=1000, repair_rounds=10, recovery_rounds=10
        ),
        expected_revision=1,
        reason="夹具",
        now=0,
    )
    queue.set_pause(False, now=0)
    yield settings, store, tasks, queue, budgets, RecoveryService(store, settings), repo, task
    store.close()


def read(k, collection, **identity):
    with k[1].read() as records:
        return dict(records.one(collection, **identity))


def test_normal_pr_wait_with_real_checkpoint_is_not_a_crash(kernel):
    settings, store, _, queue, _, recovery, _, task = kernel
    token = queue.claim(now=0)
    with checkpoint_store(settings) as saver:
        builder = StateGraph(dict)
        builder.add_node("wait", lambda state: state)
        builder.add_edge(START, "wait")
        builder.add_edge("wait", END)
        builder.compile(checkpointer=saver).invoke(
            {"value": 1}, {"configurable": {"thread_id": token.run_id}}
        )
    queue.complete(token, now=1, state="waiting", wait_reason="review")
    reports = recovery.scan_startup(now=10)
    assert reports[0]["state"] == "safe_wait"
    assert read(kernel, "global_control", singleton=1)["paused"] == 0
    assert read(kernel, "tasks", id=task)["wait_reason"] == "review"


def test_expired_run_report_and_explicit_abandon_preserve_budget_history(kernel):
    _, store, _, queue, budgets, recovery, _, task = kernel
    token = queue.claim(now=0, ttl=5)
    report = recovery.scan_startup(now=10)[0]
    assert report["state"] == "review_required"
    assert report["facts"]["safe_to_abandon"]
    assert queue.claim(now=10) is None
    # 领取尝试登记同一原因，不改变恢复对象本身。
    report = recovery.report(token.run_id, now=10)
    result = recovery.abandon(
        "abandon",
        report["id"],
        expected_basis=report["basis_sha256"],
        explanation="旧执行已核对，保留未知用量",
        now=10,
    )
    assert result["global_paused"]
    assert (
        recovery.abandon(
            "abandon",
            report["id"],
            expected_basis=report["basis_sha256"],
            explanation="旧执行已核对，保留未知用量",
            now=11,
        )
        == result
    )
    assert read(kernel, "runs", id=token.run_id)["state"] == "abandoned"
    assert read(kernel, "execution_lease", singleton=1)["holder_run_id"] is None
    view = budgets.view(task)
    assert view["used"]["active_ms"] == 0
    assert view["reserved"]["active_ms"] == 5000
    assert queue.claim(now=11) is None


def test_live_model_request_is_not_assumed_stopped_after_lease_expiry(kernel):
    _, _, _, queue, budgets, recovery, _, _ = kernel
    token = queue.claim(now=0, ttl=5)
    reservation = budgets.reserve(
        token,
        "inflight-model",
        BudgetAmounts(active_ms=1000, model_calls=1, tokens=100),
        purpose="model",
        now=0,
    )
    budgets.start(token, reservation.reservation_id, now=0)
    report = recovery.report(token.run_id, now=10)
    assert not report["facts"]["requests_safe"]
    with pytest.raises(PermissionError):
        recovery.abandon(
            "unsafe",
            report["id"],
            expected_basis=report["basis_sha256"],
            explanation="不能假定旧请求已停止",
            now=10,
        )
    assert read(kernel, "execution_lease", singleton=1)["holder_run_id"] == token.run_id


def test_unknown_remote_write_is_not_replayed_or_discarded(kernel):
    _, store, _, queue, _, recovery, _, task = kernel
    token = queue.claim(now=0)
    queue.complete(token, now=1, state="waiting", wait_reason="review")
    with store.write() as records:
        records.add(
            "publish_operations",
            {
                "id": "remote",
                "task_id": task,
                "operation_key": "create-once",
                "state": "unknown",
                "expected_json": "{}",
            },
        )
    report = recovery.scan_startup(now=2)[0]
    assert not report["facts"]["remote_safe"]
    assert read(kernel, "publish_operations", id="remote")["state"] == "unknown"
    assert read(kernel, "global_control", singleton=1)["paused"] == 1


def test_checkpoint_ahead_of_business_result_is_not_trusted(kernel):
    _, store, _, queue, _, recovery, _, _ = kernel
    token = queue.claim(now=0, ttl=5)
    with store.write() as records:
        records.add(
            "stage_intents",
            {
                "id": "stage",
                "run_id": token.run_id,
                "stage_key": "publish",
                "thread_id": token.run_id,
                "checkpoint_id": "unconfirmed",
                "state": "intent",
                "context_json": "{}",
            },
        )
    report = recovery.report(token.run_id, now=10)
    assert report["facts"]["stage_result_unknown"]
    assert not report["facts"]["safe_to_reuse_checkpoint"]
    assert report["facts"]["checkpoints"][0]["exists"] is False


def test_report_revision_changes_after_new_input_and_orphan_run_cannot_be_claimed(kernel):
    _, store, tasks, queue, _, recovery, _, task = kernel
    token = queue.claim(now=0, ttl=5)
    report = recovery.report(token.run_id, now=10)
    tasks.observe(task, "issue:1", "v2", {"body": "新范围"}, now=10)
    with pytest.raises(ConflictError):
        recovery.abandon(
            "stale",
            report["id"],
            expected_basis=report["basis_sha256"],
            explanation="旧报告不能采用",
            now=10,
        )
    with store.write() as records:
        records.change(
            "execution_lease",
            {"singleton": 1},
            {"holder_run_id": None, "expires_at": None, "heartbeat_at": None},
        )
    assert queue.claim(now=11) is None
    assert read(kernel, "runs", id=token.run_id)["state"] == "running"


@pytest.mark.skipif(os.name != "nt", reason="Windows Worker 创建身份核验")
def test_dead_worker_request_becomes_conservative_unknown_without_double_charge(kernel):
    settings, store, _, _, budgets, recovery, _, task = kernel
    script = """
import json, os, sys, time
from pathlib import Path
from repowatch.configuration import StorageSettings
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.orchestration import Orchestrator
from repowatch.application.budgets import BudgetService
from repowatch.domain.budget import BudgetAmounts
settings = StorageSettings(Path(sys.argv[1]))
store = SQLiteBusinessStore(settings)
queue = Orchestrator(store)
token = queue.claim(now=time.time(), ttl=5)
budgets = BudgetService(store, settings.artifacts_dir)
reservation = budgets.reserve(
    token, 'old-request', BudgetAmounts(active_ms=1000, model_calls=1, tokens=100),
    purpose='model', now=time.time(),
)
budgets.start(token, reservation.reservation_id, now=time.time())
print(json.dumps({
    'run_id': token.run_id, 'reservation_id': reservation.reservation_id,
}), flush=True)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(settings.data_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    identity = json.loads(completed.stdout)
    report = recovery.scan_startup(now=time.time())[0]
    assert report["facts"]["worker_dead"]
    assert report["facts"]["requests_safe"]
    recovery.abandon(
        "dead-worker",
        report["id"],
        expected_basis=report["basis_sha256"],
        explanation="已按进程创建身份核对旧 Worker 退出",
        now=time.time(),
    )
    assert read(kernel, "budget_reservations", id=identity["reservation_id"])["state"] == "unknown"
    assert budgets.view(task)["reserved"]["tokens"] == 100
    assert budgets.view(task)["used"]["model_calls"] == 0
