"""已认证的本地管理路由；模型／命令只保存受管任务，不在请求内执行。"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from repowatch.domain.budget import BudgetAmounts


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, strict=True, ge=0)
    data: dict = Field(default_factory=dict)

    def revision(self):
        if self.expected_revision is None:
            raise ValueError("操作缺少预期修订")
        return self.expected_revision


def routes(services):
    router = APIRouter(prefix="/api/v1")

    @router.get("/state")
    def state():
        return services.console.state()

    @router.get("/tasks/{task_id}")
    def task(task_id: str):
        return services.console.task(task_id)

    @router.get("/events")
    def events(after: int = 0, limit: int = 100, task_id: str | None = None):
        return services.console.events.read(after=after, limit=limit, task_id=task_id)

    @router.get("/events/stream")
    def event_stream(request: Request, after: int = 0, task_id: str | None = None):
        cursor = request.headers.get("last-event-id")
        if cursor is not None:
            if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 19:
                raise ValueError("事件游标无效")
            after = int(cursor)
        if not 0 <= after <= 9223372036854775807:
            raise ValueError("事件游标无效")
        batch = services.console.events.read(after=after, limit=100, task_id=task_id)

        def frames():
            # Bounded batches release the read transaction before streaming. EventSource
            # reconnects with Last-Event-ID; every reconnect rechecks the local session.
            yield "retry: 1500\n: bounded event batch\n\n"
            for event in batch["events"]:
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['sequence']}\nevent: business\ndata: {data}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @router.get("/providers")
    def providers():
        return services.providers.list()

    @router.post("/providers/import")
    def import_providers(body: Mutation):
        return services.providers.save(body.operation_id, body.data["payload"])

    @router.post("/providers/{provider_id}/edit")
    def edit_provider(provider_id: str, body: Mutation):
        return services.providers.save(
            body.operation_id,
            body.data["payload"],
            provider_id=provider_id,
            expected_revision=body.revision(),
        )

    @router.post("/providers/{revision_id}/models")
    def model_list(revision_id: str, body: Mutation):
        # Explicit host read; no inference request and no queue bypass.
        return services.adapter.list_models(body.operation_id, revision_id)

    @router.get("/providers/batches/{batch_id}")
    def provider_batch(batch_id: str):
        return services.providers.status(batch_id)

    @router.get("/roles")
    def roles():
        return services.roles.list()

    @router.post("/roles/{key}")
    def bind_role(key: str, body: Mutation):
        return services.roles.set_binding(
            body.operation_id,
            key,
            body.data["definition"],
            expected_revision=body.revision(),
        )

    @router.post("/control/{action}")
    def control(action: str, body: Mutation):
        if action not in {"pause", "resume"}:
            raise ValueError("未知控制操作")
        return getattr(services.control, action)(
            body.operation_id,
            expected_revision=body.revision(),
            now=services.clock(),
            repository_id=body.data.get("repository_id"),
        )

    @router.post("/tasks/{task_id}/operate")
    def operate(task_id: str, body: Mutation):
        return services.tasks.operate(
            body.operation_id,
            task_id,
            expected_revision=body.revision(),
            action=body.data["action"],
            payload=body.data.get("payload"),
            now=services.clock(),
        )

    @router.post("/reasons/{reason_id}/resolve")
    def resolve(reason_id: str, body: Mutation):
        return services.control.resolve_reason(
            body.operation_id,
            reason_id,
            expected_revision=body.revision(),
            now=services.clock(),
            explanation=body.data["explanation"],
            retry_plan=body.data.get("retry_plan"),
            retry_limits=body.data.get("retry_limits"),
        )

    @router.post("/diagnostics")
    def diagnostic(body: Mutation):
        return services.diagnostics.request(
            body.operation_id,
            body.data["kind"],
            body.data["parameters"],
            BudgetAmounts(**body.data["limits"]),
            expected_revision=body.revision(),
            now=services.clock(),
            duration=body.data.get("duration", 60),
        )

    @router.post("/tasks/{task_id}/budget/initial")
    def initial_budget(task_id: str, body: Mutation):
        return services.budgets.authorize_initial(
            body.operation_id,
            task_id,
            BudgetAmounts(**body.data["limits"]),
            expected_revision=body.revision(),
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/budget/renewals/{renewal_id}/decide")
    def renewal(renewal_id: str, body: Mutation):
        return services.budgets.decide_renewal(
            body.operation_id,
            renewal_id,
            expected_revision=body.revision(),
            decision=body.data["decision"],
            reason=body.data["reason"],
            now=services.clock(),
            increase=BudgetAmounts(**body.data["increase"]) if body.data.get("increase") else None,
        )

    @router.get("/tasks/{task_id}/budget/adjustment-basis")
    def adjustment_basis(task_id: str):
        return services.adjustments.basis(task_id)

    @router.post("/tasks/{task_id}/budget/adjust")
    def adjustment(task_id: str, body: Mutation):
        return services.adjustments.approve(
            body.operation_id,
            task_id,
            BudgetAmounts(**body.data["increase"]),
            expected_revision=body.revision(),
            expected_basis=body.data["basis_sha256"],
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/budget/renewals/{renewal_id}/refresh")
    def refresh(renewal_id: str, body: Mutation):
        return services.budgets.refresh_renewal(
            body.operation_id,
            renewal_id,
            expected_revision=body.revision(),
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/runs/{run_id}/report")
    def report(run_id: str, body: Mutation):
        # Report identity binds current database facts; it cannot replay any side effect.
        return services.recovery.report(run_id, now=services.clock())

    @router.post("/recovery/{report_id}/abandon")
    def abandon(report_id: str, body: Mutation):
        return services.recovery.abandon(
            body.operation_id,
            report_id,
            expected_basis=body.data["basis_sha256"],
            explanation=body.data["explanation"],
            now=services.clock(),
        )

    return router
