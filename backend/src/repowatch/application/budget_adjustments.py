"""不依赖活跃 Run 的人工有限加额；账本、未知占用及阻塞保持。"""

from repowatch.application.budget_accounting import budget_view, ledger_values, save_ledger
from repowatch.application.budgets import _basis, human, remember_operation, replay_operation
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import business_event, required
from repowatch.domain.budget import amounts, plus
from repowatch.domain.task_records import canonical_json, content_digest, require_time


class BudgetAdjustments:
    def __init__(self, store):
        self.store = store

    def basis(self, task_id):
        with self.store.read() as records:
            task = required(records, "tasks", id=task_id)
            owner = task["budget_owner_task_id"]
            return {
                "owner_task_id": owner,
                "basis_sha256": content_digest(_basis(records, owner, owner)),
                "ledger": budget_view(required(records, "budget_ledgers", owner_task_id=owner)),
            }

    def approve(
        self,
        operation_id,
        owner_task_id,
        increase,
        *,
        expected_revision,
        expected_basis,
        reason,
        now,
        actor="local-user",
    ):
        human(actor)
        require_time(now)
        delta = amounts(increase.as_dict(), complete=True)
        if not any(delta.values()) or not reason.strip() or len(reason) > 4000:
            raise ValueError("人工调整必须指定有限增量和处理依据")
        request = {
            "kind": "manual_budget_adjustment",
            "owner": owner_task_id,
            "increase": delta,
            "revision": expected_revision,
            "basis": expected_basis,
            "reason": reason,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            task = required(records, "tasks", id=owner_task_id)
            ledger = required(records, "budget_ledgers", owner_task_id=owner_task_id)
            if task["budget_owner_task_id"] != owner_task_id or task["mode"] == "diagnose":
                raise PermissionError("只能向业务预算归属方追加，诊断不能续期")
            if (
                ledger["revision"] != expected_revision
                or content_digest(_basis(records, owner_task_id, owner_task_id)) != expected_basis
            ):
                raise ConflictError("预算、输入或进展已变化，请刷新后确认")
            if records.find("runs", budget_owner_task_id=owner_task_id, state="running"):
                raise PermissionError("先安全停止当前执行，再确认人工预算调整")
            if ledger["revoked"] or task["cancel_requested"] or task["user_excluded"]:
                raise PermissionError("预算已撤销或任务已取消，不能用加额重新启用")
            limits, _, _ = ledger_values(ledger)
            if not limits:
                raise PermissionError("尚无初始授权，请先配置完整初始预算")
            totals = amounts(plus(limits, delta), complete=True, positive=True)
            updated = save_ledger(
                records,
                ledger,
                limits_json=canonical_json(totals),
                authorization_revision=ledger["authorization_revision"] + 1,
            )
            for proposal in records.find(
                "budget_renewals", owner_task_id=owner_task_id, state="pending"
            ):
                records.change("budget_renewals", {"id": proposal["id"]}, {"state": "stale"})
            result = {"ledger": budget_view(updated), "resume_required": True}
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            records.add(
                "budget_adjustments",
                {
                    "operation_id": operation_id,
                    "owner_task_id": owner_task_id,
                    "basis_sha256": expected_basis,
                    "increase_json": canonical_json(delta),
                    "reason": reason,
                    "previous_revision": expected_revision,
                    "resulting_revision": updated["revision"],
                    "created_at": now,
                },
            )
            business_event(records, "budget_manually_adjusted", owner_task_id, increase=delta)
            return result
