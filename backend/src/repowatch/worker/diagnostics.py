"""显式诊断的受信任处理器；不提供源码写入或任意宿主命令。"""

from repowatch.adapters.providers import ModelLimits, ProviderRequestError
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.credentials import CredentialError
from repowatch.application.task_records import business_event
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.providers import ProviderModel


class DiagnosticRunner:
    def __init__(self, diagnostics, adapter, budgets, orchestrator, *, clock):
        self.diagnostics, self.adapter = diagnostics, adapter
        self.budgets, self.orchestrator, self.clock = budgets, orchestrator, clock
        self.executor = BudgetedExecutor(budgets, orchestrator, clock=clock)

    def run(self, token):
        specification = self.diagnostics.specification(token, now=self.clock())
        kind = specification["kind"]
        if kind == "storage_probe":

            def probe():
                with self.diagnostics.store.write() as records:
                    business_event(records, "storage_probe", run_id=token.run_id)
                return True

            self.executor.execute(
                token,
                "storage-probe",
                BudgetAmounts(active_ms=1000),
                probe,
                lambda _: {},
                purpose="tool",
            )
            return self.diagnostics.finish(
                token,
                now=self.clock(),
                passed=True,
                checks={"storage": True},
            )
        if kind != "provider_connection":
            raise PermissionError("该测试需要专用角色能力核验处理器，不能用连接成功代替")
        remaining = min(30, specification["deadline"] - self.clock())
        if remaining <= 0:
            raise PermissionError("诊断期限已到，不再请求供应商")
        parameters = specification["parameters"]
        passed = False
        try:
            with self.adapter.session(
                parameters["provider_revision_id"],
                ProviderModel(model=parameters["model"]),
                ModelLimits(timeout_seconds=remaining, max_output_tokens=32),
            ) as model:

                def usage(message):
                    result = {"model_calls": 1}
                    total = (message.usage_metadata or {}).get("total_tokens")
                    if type(total) is int and total >= 0:
                        result["tokens"] = total
                    return result

                answer = self.executor.execute(
                    token,
                    "provider-connection",
                    BudgetAmounts(
                        active_ms=max(1, round(remaining * 1000)),
                        model_calls=1,
                        tokens=256,
                    ),
                    lambda: model.invoke("Reply with OK."),
                    usage,
                )
                # A bounded reasoning response can exhaust 32 output tokens before text.
                # This establishes only a valid model response, never role readiness.
                passed = answer.type == "ai"
        except (ProviderRequestError, CredentialError):
            pass
        return self.diagnostics.finish(
            token,
            now=self.clock(),
            passed=passed,
            checks={"connection": passed},
        )
