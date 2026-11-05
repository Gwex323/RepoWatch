"""预算的事务内计算。有效时长只按唯一执行者的持久时钟累计一次。"""

import json

from repowatch.application.task_records import block, bump_task, required
from repowatch.domain.budget import (
    DIMENSIONS,
    amounts,
    available,
    is_exhausted,
    plus,
    vector,
)
from repowatch.domain.task_records import canonical_json, require_time, stable_id


def ledger_values(ledger):
    limits = json.loads(ledger["limits_json"])
    if limits:
        limits = amounts(limits, complete=True, positive=True)
    return (
        limits,
        vector(json.loads(ledger["used_json"])),
        vector(json.loads(ledger["reserved_json"])),
    )


def warning(limits, used, reserved, percent):
    return bool(limits) and any(
        (used[key] + reserved[key]) * 100 >= limits[key] * percent for key in DIMENSIONS
    )


def save_ledger(records, ledger, *, used=None, reserved=None, **changes):
    if used is not None:
        changes["used_json"] = canonical_json(amounts(used, complete=True))
    if reserved is not None:
        changes["reserved_json"] = canonical_json(amounts(reserved, complete=True))
    changes["revision"] = ledger["revision"] + 1
    records.change("budget_ledgers", {"owner_task_id": ledger["owner_task_id"]}, changes)
    return required(records, "budget_ledgers", owner_task_id=ledger["owner_task_id"])


def mark_budget_block(records, run, code, now):
    reason = records.one("block_reasons", id=stable_id("block", run["task_id"], code))
    if reason is None or reason["resolved"]:
        block(records, code, run["task_id"], now=now, owner_task_id=run["budget_owner_task_id"])
    task = required(records, "tasks", id=run["task_id"])
    if task["budget_wait_reason"] != code or not task["stop_requested"]:
        changes = {"budget_wait_reason": code, "stop_requested": 1}
        if task["state"] not in {"running", "ended", "cancelled"} and not (
            task["user_excluded"] or task["cancel_requested"]
        ):
            changes.update(state="blocked", wait_reason=None, outcome=code)
        bump_task(records, task, **changes)


def meter_execution(records, run, now):
    require_time(now)
    # Delegated roles share the authority's one durable active-time clock.
    depth = 0
    while run["parent_run_id"] is not None:
        run = required(records, "runs", id=run["parent_run_id"])
        depth += 1
        if depth > 3:
            raise PermissionError("运行委派链无效")
    ledger = required(records, "budget_ledgers", owner_task_id=run["budget_owner_task_id"])
    tick = round(now * 1000)
    previous = run["budget_metered_ms"]
    if previous is None:
        previous = round((run["started_at"] if run["started_at"] is not None else now) * 1000)
    if tick < previous:
        raise PermissionError("宿主计时回退，必须核对运行，不能重置累计时长")
    elapsed = tick - previous if run["budget_clock_state"] == "active" else 0
    records.change("runs", {"id": run["id"]}, {"budget_metered_ms": tick})
    limits, used, reserved = ledger_values(ledger)
    if elapsed and limits:
        used = plus(used, {"active_ms": elapsed})
        for reservation in records.find(
            "budget_reservations", owner_task_id=run["budget_owner_task_id"], state="dispatched"
        ):
            if reservation["purpose"] == "counter":
                continue
            outstanding = vector(json.loads(reservation["outstanding_json"]))
            covered = min(outstanding["active_ms"], elapsed)
            outstanding["active_ms"] -= covered
            reserved["active_ms"] -= covered
            measured = reservation["metered_active_ms"] + elapsed
            records.change(
                "budget_reservations",
                {"id": reservation["id"]},
                {
                    "outstanding_json": canonical_json(outstanding),
                    "metered_active_ms": measured,
                },
            )
            if measured > json.loads(reservation["upper_json"])["active_ms"]:
                mark_budget_block(records, run, "budget_bound_exceeded", now)
        ledger = save_ledger(records, ledger, used=used, reserved=reserved)
    if is_exhausted(limits, used):
        mark_budget_block(records, run, "budget_exhausted", now)
    return ledger


def reservation_guard(records, run, reservation_id, purpose):
    reservation = records.one("budget_reservations", id=reservation_id) if reservation_id else None
    if (
        reservation is None
        or reservation["run_id"] != run["id"]
        or reservation["generation"] != run["generation"]
        or reservation["owner_task_id"] != run["budget_owner_task_id"]
        or reservation["purpose"] != purpose
        or reservation["state"] != "dispatched"
    ):
        raise PermissionError("当前执行用途没有已预留、绑定本运行的预算")


def budget_view(ledger, *, warning_percent=80):
    limits, used, reserved = ledger_values(ledger)
    status = (
        "revoked"
        if ledger["revoked"]
        else "configuration_required"
        if not limits
        else "exhausted"
        if is_exhausted(limits, used)
        else "available"
    )
    return {
        "owner_task_id": ledger["owner_task_id"],
        "revision": ledger["revision"],
        "authorization_revision": ledger["authorization_revision"],
        "initial_limits": json.loads(ledger["initial_limits_json"]),
        "approved_progress_ids": json.loads(ledger["approved_progress_json"]),
        "limits": limits,
        "used": used,
        "reserved": reserved,
        "available": available(limits, used, reserved) if limits else vector({}),
        "status": status,
        "warning_percent": warning_percent,
        "warning": warning(limits, used, reserved, warning_percent),
    }
