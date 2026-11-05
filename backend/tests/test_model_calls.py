import json

import httpx
import pytest
from test_roles import bind, diagnose, task_run
from test_roles import kernel as kernel

from repowatch.adapters.providers import ModelLimits, ProviderAdapter, ProviderRequestError
from repowatch.application.provider_calls import ModelCalls
from repowatch.application.storage import ConflictError
from repowatch.domain.budget import BudgetAmounts, BudgetDenied
from repowatch.worker.model_calls import managed_role_model


class Clock:
    value = 0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds


def service(k, *, roles=None):
    clock = Clock()
    return ModelCalls(
        k.store, k.budgets, k.queue, k.artifacts, clock=clock.now, wait=clock.wait, roles=roles
    ), clock


def invoke(calls, token, operation, *, key="step", upper=None):
    return calls.invoke(
        token,
        key,
        {"messages": "controlled"},
        upper or BudgetAmounts(active_ms=1000, model_calls=1, tokens=100),
        operation,
        lambda result: {"model_calls": 1, "tokens": result["tokens"]},
        lambda result: json.dumps(result).encode(),
        lambda raw: json.loads(raw),
    )


def test_transient_retry_is_counted_and_backoff_not_billed_or_replayed(kernel):
    k = kernel
    task, token = task_run(k)
    calls, clock = service(k)
    attempts = []

    def operation():
        attempts.append(True)
        clock.value += 0.1
        if len(attempts) == 1:
            raise ProviderRequestError("timeout", transient=True)
        return {"tokens": 8, "text": "ok"}

    result = invoke(calls, token, operation)
    assert result["text"] == "ok"
    view = k.budgets.view(task)
    assert view["used"]["model_calls"] == 2
    assert view["used"]["active_ms"] == 200
    assert view["used"]["tokens"] == 8
    assert view["reserved"]["tokens"] == 100
    assert clock.value == pytest.approx(1.2)
    assert invoke(calls, token, operation) == result
    assert len(attempts) == 2
    with pytest.raises(ConflictError):
        invoke(
            calls, token, operation, upper=BudgetAmounts(active_ms=1000, model_calls=1, tokens=200)
        )


@pytest.mark.parametrize(
    "code,transient,expected",
    [
        ("provider_http_error", True, 3),
        ("quota_exhausted", False, 1),
        ("authentication_or_permission", False, 1),
    ],
)
def test_persistent_or_quota_failure_pauses_without_fallback(kernel, code, transient, expected):
    k = kernel
    task, token = task_run(k)
    calls, _ = service(k)
    attempts = []

    def failed():
        attempts.append(True)
        raise ProviderRequestError(code, transient=transient)

    with pytest.raises(ProviderRequestError):
        invoke(calls, token, failed)
    assert len(attempts) == expected
    assert k.budgets.view(task)["used"]["model_calls"] == expected
    with k.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert records.find("provider_invocations")[0]["state"] == "failed"
    with pytest.raises(ProviderRequestError):
        invoke(calls, token, failed)
    assert len(attempts) == expected


def test_insufficient_budget_does_not_issue_or_relabel_a_model_attempt(kernel):
    k = kernel
    task, token = task_run(k)
    calls, _ = service(k)
    attempts = []
    with pytest.raises(BudgetDenied):
        invoke(
            calls,
            token,
            lambda: attempts.append(True),
            upper=BudgetAmounts(
                active_ms=1000,
                model_calls=1,
                tokens=100001,
            ),
        )
    assert attempts == []
    assert k.budgets.view(task)["used"]["model_calls"] == 0
    with k.store.read() as records:
        assert records.find("provider_invocations")[0]["state"] == "waiting"


def test_unknown_result_is_not_implicitly_retried_after_exception(kernel):
    k = kernel
    _, token = task_run(k)
    calls, _ = service(k)

    def crashed():
        raise RuntimeError("injected unconfirmed local result")

    with pytest.raises(RuntimeError):
        invoke(calls, token, crashed)
    with pytest.raises(ConflictError):
        invoke(calls, token, crashed)


def test_sdk_wrapper_meters_each_http_attempt_and_preserves_tool_binding(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    task, token = task_run(k)
    calls, _ = service(k, roles=k.roles)
    requests = []

    def wire(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "model"
        assert body["parallel_tool_calls"] is False
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "test-role-key"}})
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    adapter = ProviderAdapter(k.providers, transport=httpx.MockTransport(wire))
    with managed_role_model(
        adapter,
        calls,
        token,
        "processor",
        "node",
        limits=ModelLimits(timeout_seconds=1, max_output_tokens=32),
    ) as model:
        tool = {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object", "properties": {}}},
        }
        assert model.bind_tools([tool]).invoke("test").text == "ok"
    assert len(requests) == 2
    assert k.budgets.view(task)["used"]["model_calls"] == 2
    assert k.budgets.view(task)["used"]["tokens"] == 8
