"""同一运行与调用意图内有限重试；每次实际尝试共享原预算。"""

import math
from dataclasses import dataclass

from repowatch.adapters.providers import ProviderRequestError
from repowatch.application.budget_accounting import meter_execution
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, business_event, execution_record
from repowatch.domain.budget import BudgetDenied
from repowatch.domain.task_records import content_digest, stable_id


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 1

    def __post_init__(self):
        if (
            type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 3
            or not math.isfinite(self.backoff_seconds)
            or not 0 <= self.backoff_seconds <= 30
        ):
            raise ValueError("模型重试必须有限且至多三次")


class ModelCalls:
    def __init__(self, store, budgets, queue, artifacts, *, clock, wait, roles=None, safety=None):
        self.store, self.queue, self.artifacts = store, queue, artifacts
        self.clock, self.wait, self.roles = clock, wait, roles
        self.executor = BudgetedExecutor(budgets, queue, clock=clock)
        self.safety = safety

    def invoke(
        self,
        token,
        call_key,
        request,
        upper,
        operation,
        usage,
        encode,
        decode,
        *,
        role_key=None,
        policy=None,
        preallocated_id=None,
    ):
        policy = policy or RetryPolicy()
        if not call_key:
            raise ValueError("模型调用必须有稳定意图标识")
        identifier = stable_id("model-call", token.run_id, call_key)
        fingerprint = content_digest(
            {
                "request": request,
                "policy": {
                    "max_attempts": policy.max_attempts,
                    "backoff_seconds": policy.backoff_seconds,
                },
                "upper": upper.as_dict(),
                "role_key": role_key,
            }
        )
        with self.store.write() as records:
            execution_record(records, token, self.clock())
            existing = records.one("provider_invocations", id=identifier)
            if existing:
                if existing["request_sha256"] != fingerprint:
                    raise ConflictError("同一模型意图的内容或策略不同")
                if existing["state"] == "succeeded":
                    cached = existing["artifact_id"]
                elif existing["state"] == "failed":
                    raise ProviderRequestError(existing["error_code"])
                elif existing["state"] != "waiting":
                    raise ConflictError("上次调用结果尚未确认，禁止盲目重放")
                else:
                    cached = None
                attempts = existing["attempts"]
            else:
                cached, attempts = None, 0
                records.add(
                    "provider_invocations",
                    {
                        "id": identifier,
                        "run_id": token.run_id,
                        "call_key": call_key,
                        "request_sha256": fingerprint,
                        "state": "pending",
                    },
                )
        if cached:
            artifact = self.artifacts.require(cached, run_id=token.run_id, now=self.clock())
            return decode((self.artifacts.root / artifact["relative_path"]).read_bytes())
        for attempt in range(attempts + 1, policy.max_attempts + 1):
            if self.safety:
                self.safety.preflight(token)
            if self.roles is not None and role_key is not None:
                self.roles.admit(token, now=self.clock(), keys=(role_key,))

            def actual_attempt(attempt=attempt):
                if self.safety:
                    self.safety.touch(token)
                with self.store.write() as records:
                    execution_record(records, token, self.clock())
                    records.change(
                        "provider_invocations",
                        {"id": identifier},
                        {
                            "state": "pending",
                            "attempts": attempt,
                        },
                    )
                return operation()

            try:
                result = self.executor.execute(
                    token,
                    f"model:{identifier}:{attempt}",
                    upper,
                    actual_attempt,
                    usage,
                    preallocated_id=preallocated_id if attempt == 1 else None,
                )
            except BudgetDenied:
                with self.store.write() as records:
                    reservation = records.one(
                        "budget_reservations",
                        run_id=token.run_id,
                        operation_key=f"model:{identifier}:{attempt}",
                    )
                    records.change(
                        "provider_invocations",
                        {"id": identifier},
                        {
                            "state": "unknown" if reservation else "waiting",
                        },
                    )
                raise
            except ProviderRequestError as error:
                retry = error.transient and attempt < policy.max_attempts
                with self.store.write() as records:
                    run = execution_record(records, token, self.clock())
                    business_event(
                        records,
                        "provider_attempt_failed",
                        run["task_id"],
                        call_id=identifier,
                        attempt=attempt,
                        error=error.code,
                        retry=retry,
                    )
                    if not retry:
                        records.change(
                            "provider_invocations",
                            {"id": identifier},
                            {
                                "state": "failed",
                                "error_code": error.code,
                            },
                        )
                        block(records, "provider_" + error.code, run["task_id"], now=self.clock())
                if not retry:
                    raise
                self._backoff(token, policy.backoff_seconds)
                continue
            artifact_id = self.artifacts.register(
                token,
                "model-result:" + identifier,
                encode(result),
                context={"producer": "host_model_adapter", "call_id": identifier},
                now=self.clock(),
            )
            with self.store.write() as records:
                execution_record(records, token, self.clock())
                records.change(
                    "provider_invocations",
                    {"id": identifier},
                    {
                        "state": "succeeded",
                        "artifact_id": artifact_id,
                    },
                )
            return result
        raise PermissionError("模型重试上限已经耗尽")

    def _backoff(self, token, seconds):
        authority_id = token.authority_run_id or token.run_id
        with self.store.write() as records:
            run = execution_record(records, token, self.clock())
            meter_execution(records, run, self.clock())
            records.change("runs", {"id": authority_id}, {"budget_clock_state": "backoff"})
        remaining = seconds
        try:
            while remaining > 0:
                part = min(remaining, 5)
                self.queue.heartbeat(token, now=self.clock(), ttl=30)
                self.wait(part)
                remaining -= part
        finally:
            with self.store.write() as records:
                run = execution_record(records, token, self.clock())
                meter_execution(records, run, self.clock())
                records.change("runs", {"id": authority_id}, {"budget_clock_state": "active"})
