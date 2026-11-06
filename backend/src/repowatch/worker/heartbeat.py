"""长模型／工具调用之外的短事务续约；失败后禁止后续执行。"""

import threading
import time
from contextlib import AbstractContextManager


class ExecutionHeartbeat(AbstractContextManager):
    def __init__(self, queue, token, clock, *, interval=5):
        self.queue, self.token, self.clock, self.interval = queue, token, clock, interval
        self.stopped = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="repowatch-heartbeat")

    def _loop(self):
        while not self.stopped.wait(self.interval):
            self._beat()

    def _beat(self):
        try:
            self.queue.heartbeat(self.token, ttl=30, clock=self.clock)
        except PermissionError:
            # Retry once for a transient lease or storage failure; a genuine
            # rollback or lost lease still fails on the retry.
            time.sleep(0.05)
            try:
                self.queue.heartbeat(self.token, ttl=30, clock=self.clock)
            except Exception as error:
                self.error = error
                self.stopped.set()
        except Exception as error:
            self.error = error
            self.stopped.set()

    def check(self):
        if self.error is None:
            return
        # Completion releases the lease before this context exits, so a beat
        # that loses the lease after a successful finish is not a failure.
        try:
            with self.queue.store.read() as records:
                run = records.one("runs", id=self.token.run_id)
        except Exception:
            run = None
        if run is not None and run["state"] == "running":
            raise PermissionError("续约失败，先停止并核对旧运行") from None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stopped.set()
        self.thread.join(timeout=5)
        return False
