"""长模型／工具调用之外的短事务续约；失败后禁止后续执行。"""

import threading
from contextlib import AbstractContextManager


class ExecutionHeartbeat(AbstractContextManager):
    def __init__(self, queue, token, clock, *, interval=5):
        self.queue, self.token, self.clock, self.interval = queue, token, clock, interval
        self.stopped = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="repowatch-heartbeat")

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self.queue.heartbeat(self.token, now=self.clock(), ttl=30)
            except Exception as error:
                self.error = error
                self.stopped.set()

    def check(self):
        if self.error is not None:
            raise PermissionError("续约失败，先停止并核对旧运行") from None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stopped.set()
        self.thread.join(timeout=5)
        return False
