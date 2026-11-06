from types import SimpleNamespace

import pytest

from repowatch.worker.heartbeat import ExecutionHeartbeat


class Queue:
    def __init__(self, errors, run_state="running"):
        self.errors = list(errors)
        self.calls = []
        self.store = SimpleNamespace(read=self._read)
        self.run_state = run_state

    def _read(self):
        run = {"state": self.run_state}

        class Records:
            def one(self, collection, **identity):
                return run

        class Context:
            def __enter__(self):
                return Records()

            def __exit__(self, *_args):
                return False

        return Context()

    def heartbeat(self, token, *, now=None, ttl=30, clock=None):
        if now is None and clock is not None:
            now = clock()
        self.calls.append(now)
        if self.errors:
            raise self.errors.pop(0)


def heartbeat(queue):
    ticks = iter([1.0, 1.1, 1.2])
    return ExecutionHeartbeat(queue, SimpleNamespace(run_id="run"), lambda: next(ticks))


def test_stale_clock_sample_is_retried_with_a_fresh_sample():
    queue = Queue([PermissionError("stale sample")])
    beat = heartbeat(queue)
    beat._beat()
    assert beat.error is None
    assert queue.calls == [1.0, 1.1]
    beat.check()


def test_persistent_renewal_failure_blocks_a_running_run():
    queue = Queue([PermissionError("rollback"), PermissionError("rollback")])
    beat = heartbeat(queue)
    beat._beat()
    assert beat.error is not None
    with pytest.raises(PermissionError, match="续约失败"):
        beat.check()


def test_renewal_failure_after_completion_is_not_fatal():
    queue = Queue([PermissionError("released"), PermissionError("released")], run_state="ended")
    beat = heartbeat(queue)
    beat._beat()
    assert beat.error is not None
    beat.check()
