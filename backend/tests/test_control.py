import json
import os
import subprocess
import sys
import time

import pytest

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.budgets import BudgetService
from repowatch.application.control import ControlService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords, block
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.operations.migrate import migrate
from repowatch.worker.commands import CommandSupervisor


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repo = tasks.register_repository("r", admission_mode="all_bugs")
    now = time.time()
    task = tasks.ensure_task(repo, "issue:1", issue_id="i", now=now)
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "initial",
        task,
        BudgetAmounts(
            active_ms=100_000, model_calls=10, tokens=1000, repair_rounds=10, recovery_rounds=10
        ),
        expected_revision=1,
        reason="有限夹具",
        now=now,
    )
    queue.set_pause(False, now=now)
    supervisor = CommandSupervisor(store, settings.data_dir / "commands", queue)
    yield (
        store,
        tasks,
        queue,
        budgets,
        supervisor,
        ControlService(store, commands=supervisor),
        repo,
        task,
    )
    supervisor.close()
    store.close()


def row(kernel, collection, **identity):
    with kernel[0].read() as records:
        return dict(records.one(collection, **identity))


def test_pause_versions_and_replay_do_not_resume_queue(kernel):
    store, _, queue, _, _, control, repo, _ = kernel
    revision = row(kernel, "global_control", singleton=1)["revision"]
    first = control.pause("pause", expected_revision=revision, now=time.time())
    assert control.pause("pause", expected_revision=revision, now=time.time()) == first
    with pytest.raises(ConflictError):
        control.pause("stale", expected_revision=revision, now=time.time())
    assert queue.claim(now=time.time()) is None
    control.resume("resume", expected_revision=first["revision"], now=time.time())
    token = queue.claim(now=time.time())
    repository_revision = row(kernel, "repositories", id=repo)["revision"]
    control.pause(
        "repo-pause", expected_revision=repository_revision, repository_id=repo, now=time.time()
    )
    queue.complete(token, state="waiting", wait_reason="safe_stop", now=time.time())
    assert row(kernel, "global_control", singleton=1)["paused"] == 0
    assert queue.claim(now=time.time()) is None


def test_no_progress_requires_substantive_bounded_plan_and_preserves_attempts(kernel):
    store, _, queue, _, _, control, _, task = kernel
    token = queue.claim(now=time.time())
    with store.write() as records:
        records.add(
            "progress_attempts",
            {
                "id": "old",
                "task_id": task,
                "run_id": token.run_id,
                "objective_key": "root-cause",
                "fingerprint": "same-failure",
                "evidence_json": "{}",
            },
        )
        block(records, "no_progress", task, now=time.time(), attempts=["old"])
    queue.complete(token, state="blocked", now=time.time())
    with store.read() as records:
        reason = dict(records.find("block_reasons", resolved=0)[0])
    with pytest.raises(ValueError):
        control.resolve_reason(
            "mere-confirmation",
            reason["id"],
            expected_revision=reason["revision"],
            explanation="已处理",
            now=time.time(),
        )
    result = control.resolve_reason(
        "retry-plan",
        reason["id"],
        expected_revision=reason["revision"],
        explanation="已缩小验证范围并更换调查策略",
        retry_plan={
            "steps": ["先验证输入边界再复现"],
            "success_condition": "取得新的可验证边界反证",
        },
        retry_limits={"max_attempts": 1, "max_steps": 5, "max_active_ms": 5000},
        now=time.time(),
    )
    assert result["global_paused"]
    assert row(kernel, "progress_attempts", id="old")["fingerprint"] == "same-failure"
    control.resume(
        "explicit-resume",
        expected_revision=row(kernel, "global_control", singleton=1)["revision"],
        now=time.time(),
    )
    current = queue.claim(now=time.time())
    snapshot = json.loads(row(kernel, "runs", id=current.run_id)["snapshot_json"])
    assert snapshot["retry_authorization"]["limits"]["max_attempts"] == 1
    queue.complete(current, state="queued", now=time.time())
    third = queue.claim(now=time.time())
    assert "retry_authorization" not in json.loads(
        row(kernel, "runs", id=third.run_id)["snapshot_json"]
    )


def test_cancel_and_resolution_do_not_clear_another_reason(kernel):
    store, _, queue, _, _, control, _, task = kernel
    token = queue.claim(now=time.time())
    with store.write() as records:
        block(records, "no_progress", task, now=time.time())
        block(records, "permission", task, now=time.time())
    control.cancel(
        "cancel", task, expected_revision=row(kernel, "tasks", id=task)["revision"], now=time.time()
    )
    queue.complete(token, now=time.time())
    with store.read() as records:
        reason = dict(records.find("block_reasons", code="no_progress")[0])
    control.resolve_reason(
        "confirm-cancel",
        reason["id"],
        expected_revision=reason["revision"],
        explanation="核对取消及旧执行停止",
        now=time.time(),
    )
    with pytest.raises(PermissionError):
        control.resume(
            "still-blocked",
            expected_revision=row(kernel, "global_control", singleton=1)["revision"],
            now=time.time(),
        )
    assert row(kernel, "global_control", singleton=1)["paused"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows 作业对象实测")
@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_actual_process_tree_must_stop_before_slot_release(kernel, tmp_path, action):
    _, _, queue, budgets, supervisor, control, _, task = kernel
    token = queue.claim(now=time.time(), ttl=300)
    reservation = budgets.reserve(
        token, "trusted-process", BudgetAmounts(active_ms=10_000), purpose="tool", now=time.time()
    )
    budgets.start(token, reservation.reservation_id, now=time.time())
    output = tmp_path / "grandchild-started"
    child_code = (
        f"from pathlib import Path; import time; Path({str(output)!r}).write_text('started'); "
        "time.sleep(30)"
    )
    parent_code = (
        f"import subprocess,sys; subprocess.Popen([sys.executable,'-I','-S','-c',{child_code!r}])"
    )
    command = supervisor.start(
        token,
        reservation.reservation_id,
        [sys.executable, "-I", "-S", "-c", parent_code],
        timeout=8,
    )
    deadline = time.monotonic() + 5
    while not output.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("夹具子进程未启动")
        time.sleep(0.01)
    assert supervisor.inspect(command)["active_processes"] > 0
    control.pause(
        "pause-live-command",
        expected_revision=row(kernel, "global_control", singleton=1)["revision"],
        now=time.time(),
    )
    if action == "cancel":
        control.cancel(
            "cancel-live-command",
            task,
            expected_revision=row(kernel, "tasks", id=task)["revision"],
            now=time.time(),
        )
    with pytest.raises(PermissionError):
        queue.complete(token, now=time.time())
    assert row(kernel, "execution_lease", singleton=1)["holder_run_id"] == token.run_id
    assert supervisor.stop(command)["active_processes"] == 0
    budgets.settle(token, reservation.reservation_id, {}, now=time.time())
    queue.complete(token, now=time.time(), state="waiting", wait_reason="safe_stop")
    assert row(kernel, "execution_lease", singleton=1)["holder_run_id"] is None
    assert queue.claim(now=time.time()) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows 作业对象实测")
def test_controller_crash_kills_job_and_restarted_inspector_verifies_it(kernel, tmp_path):
    store, _, queue, budgets, supervisor, _, _, _ = kernel
    token = queue.claim(now=time.time(), ttl=300)
    reservation = budgets.reserve(
        token, "crash-controller", BudgetAmounts(active_ms=10_000), purpose="tool", now=time.time()
    )
    budgets.start(token, reservation.reservation_id, now=time.time())
    marker, identity = tmp_path / "started", tmp_path / "identity"
    script = """
import json, os, sys, time
from pathlib import Path
from repowatch.configuration import StorageSettings
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.orchestration import Orchestrator
from repowatch.domain.task_records import ExecutionToken
from repowatch.worker.commands import CommandSupervisor
settings = StorageSettings(Path(sys.argv[1]))
store = SQLiteBusinessStore(settings)
manager = CommandSupervisor(store, settings.data_dir / 'commands', Orchestrator(store))
target = Path(sys.argv[5])
code = ('from pathlib import Path; import time; Path(%r).write_text("started"); '
        'time.sleep(30)') % str(target)
identifier = manager.start(ExecutionToken(sys.argv[2], int(sys.argv[3])), sys.argv[4],
    [sys.executable, '-I', '-S', '-c', code], timeout=8)
Path(sys.argv[6]).write_text(identifier)
deadline = time.monotonic() + 5
while not target.exists():
    if time.monotonic() > deadline:
        raise TimeoutError('fixture did not start')
    time.sleep(0.01)
os._exit(0)
"""
    subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            script,
            str(store.settings.data_dir),
            token.run_id,
            str(token.generation),
            reservation.reservation_id,
            str(marker),
            str(identity),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    identifier = identity.read_text()
    deadline = time.monotonic() + 3
    proof = supervisor.inspect(identifier)
    while not proof["stopped"] and time.monotonic() < deadline:
        time.sleep(0.01)
        proof = supervisor.inspect(identifier)
    assert proof["stopped"] and proof["identity_verified"]
    budgets.settle(token, reservation.reservation_id, {}, now=time.time())
    queue.complete(token, now=time.time())


@pytest.mark.skipif(os.name != "nt", reason="Windows 作业对象实测")
def test_bound_command_timeout_stops_the_entire_job(kernel):
    _, _, queue, budgets, supervisor, _, _, _ = kernel
    token = queue.claim(now=time.time())
    reservation = budgets.reserve(
        token, "timeout", BudgetAmounts(active_ms=3000), purpose="tool", now=time.time()
    )
    budgets.start(token, reservation.reservation_id, now=time.time())
    identifier = supervisor.start(
        token,
        reservation.reservation_id,
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        timeout=0.5,
    )
    proof = supervisor.wait(identifier)
    assert proof["timed_out"] and proof["stopped"] and proof["active_processes"] == 0
    assert json.loads(row(kernel, "command_states", id=identifier)["evidence_json"])["timed_out"]
    budgets.settle(token, reservation.reservation_id, {}, now=time.time())
