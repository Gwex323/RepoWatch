import pytest

from repowatch.runtime.remote_recovery import (
    RecoveryController,
    SimulatedRemote,
    initialize,
    run_experiment,
)


def build(tmp_path):
    database = tmp_path / "recovery.sqlite3"
    initialize(database)
    remote = SimulatedRemote(database)
    return RecoveryController(database, remote), remote


def test_lost_ack_reconciles_without_resend(tmp_path):
    controller, remote = build(tmp_path)
    controller.set_paused(False)
    controller.record_intent("candidate-1:create-pr")
    remote.lose_ack = True
    outcome = controller.publish(title="Fix", head="branch", base="main")
    assert outcome["outcome"] == "unknown"
    assert controller.state().paused is True
    assert remote.find("candidate-1:create-pr")["id"] == "pr-candidate-1:create-pr"
    with pytest.raises(PermissionError):
        controller.publish(title="Fix", head="branch", base="main")
    assert remote.create_calls == 1
    reconciled = controller.reconcile()
    assert reconciled["outcome"] == "confirmed"
    assert controller.state().paused is True
    assert remote.create_calls == 1


def test_old_command_blocks_resume_and_stale_generation_is_rejected(tmp_path):
    controller, remote = build(tmp_path)
    controller.set_paused(False)
    controller.record_intent("candidate-2:create-pr")
    controller.publish(title="Fix", head="branch", base="main")
    controller.set_paused(True)
    with pytest.raises(PermissionError):
        controller.resume(
            old_command_alive=True,
            checkpoint_verified=True,
            artifacts_verified=True,
            remote_verified=True,
        )
    old_generation = controller.state().generation
    new_generation = controller.resume(
        old_command_alive=False,
        checkpoint_verified=True,
        artifacts_verified=True,
        remote_verified=True,
    )
    assert new_generation == old_generation + 1
    assert controller.late_completion(old_generation, {}) == {
        "accepted": False,
        "reason": "stale_generation",
    }
    assert controller.late_completion(new_generation, {"ok": True})["accepted"] is True
    assert remote.create_calls == 1


def test_normal_wait_and_diagnostic_do_not_resume(tmp_path):
    controller, _ = build(tmp_path)
    controller.set_paused(False)
    controller.record_intent("candidate-3:create-pr")
    controller.publish(title="Fix", head="branch", base="main")
    assert controller.classify_restart(old_command_alive=False) == "normal_wait"
    controller.set_paused(True)
    diagnostic = controller.run_diagnostic()
    assert diagnostic == {
        "ran": True,
        "paused_before": True,
        "paused_after": True,
        "generation_unchanged": True,
    }
    assert controller.state().paused is True


def test_scenario_report_covers_e8_and_e18():
    report = run_experiment()
    assert report["status"] == "passed"
    assert all(report["checks"].values())
    assert report["remote"]["pr_count"] == 1
    assert report["remote"]["create_calls"] == 1
    assert report["duration_seconds"] is not None
