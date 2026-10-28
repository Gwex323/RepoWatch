"""Opt-in evidence-bound coordination, with no authority beyond existing recovery."""

import asyncio
import json
import logging
import time

from . import recovery
from .coordination_model import Plan
from .fault_contract import diagnostic, note, token
from .model_providers import ModelProviders
from .roles import COORDINATOR
from .store import Conflict, Missing, uid
from .system_faults import SystemFaults, transition

LOG = logging.getLogger(__name__)
MAX_REQUESTS = 2
DEADLINE = 250


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS coordination_configuration (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1, enabled_since REAL,
            fallback TEXT, fallback_authorized INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO coordination_configuration(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS coordination_jobs (
            id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL UNIQUE
                REFERENCES system_fault_occurrences(id),
            root_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            configuration_version INTEGER NOT NULL, basis TEXT NOT NULL,
            owner TEXT NOT NULL, lease REAL NOT NULL, action_id TEXT NOT NULL,
            packet TEXT NOT NULL, plan TEXT, error TEXT, receipt TEXT,
            requests INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coordination_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL
                REFERENCES coordination_jobs(id), version INTEGER NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
            UNIQUE(job_id,version)
        );
    """)


def settings(db):
    row = dict(db.execute("SELECT * FROM coordination_configuration WHERE id=1").fetchone())
    return {
        **row,
        "enabled": bool(row["enabled"]),
        "fallback": json.loads(row["fallback"]) if row["fallback"] else None,
        "fallback_authorized": bool(row["fallback_authorized"]),
        "max_requests": MAX_REQUESTS,
        "max_seconds": DEADLINE,
    }


def model_basis(db, config):
    row = ModelProviders.role_rows(db)[COORDINATOR]
    keys = ("config", "encrypted_key", "version", "provider_id", "provider_version")
    fallback = ModelProviders.resolve(db, config["fallback"]) if config["fallback"] else None
    return recovery.digest([{k: row[k] for k in keys}, fallback])


def source_basis(db, occurrence, repairs):
    payload = json.loads(occurrence["payload"])
    table = {
        "analysis": "repository_analyses",
        "repair": "repair_tasks",
        "acceptance": "scope_acceptance_runs",
    }.get(occurrence["flow"])
    source = (
        db.execute(f"SELECT * FROM {table} WHERE id=?", (occurrence["task_id"],)).fetchone()
        if table
        else None
    )
    binding = repairs._recovery_binding(db, source) if source and table == "repair_tasks" else None
    snapshot = dict(source) if source else None
    if snapshot and table == "scope_acceptance_runs":
        snapshot.pop("model_calls", None)
    return recovery.digest([payload, snapshot, binding])


def source_budget(db, occurrence, *, reserve=False, capacity=False, maintenance=False):
    flow, task_id = occurrence["flow"], occurrence["task_id"]
    table = {
        "analysis": "repository_analyses",
        "repair": "repair_tasks",
        "acceptance": "scope_acceptance_runs",
    }.get(flow)
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (task_id,)).fetchone() if table else None
    state = row["state" if flow == "acceptance" else "status"] if row else None
    if state in {"cancelled", "stale", "completed", "pr_created"}:
        raise Conflict("coordination_source_inactive")
    if flow == "analysis":
        from .report_scheduler import MAX_REQUESTS
        from .report_scheduler import row as recovery_row

        saved = recovery_row(db, task_id)
        if saved and (
            saved["deadline"] <= time.time()
            or saved["stop_reason"] and not (maintenance and saved["stop_reason"] in {
                "non_recoverable", "review_not_started", "checkpoint_invalid",
            })
            or saved["requests"] + int(reserve or capacity) > MAX_REQUESTS
        ):
            raise Conflict("coordination_source_budget_exhausted")
        if reserve and saved:
            db.execute(
                "UPDATE analysis_report_recovery SET requests=requests+1 WHERE analysis_id=?",
                (task_id,),
            )
    elif flow == "acceptance" and row:
        from .acceptance import MAX_MODEL_REQUESTS

        if (
            row["deadline"]
            and row["deadline"] <= time.time()
            or row["model_calls"] + int(reserve or capacity) > MAX_MODEL_REQUESTS
        ):
            raise Conflict("coordination_source_budget_exhausted")
        if reserve:
            db.execute(
                "UPDATE scope_acceptance_runs SET model_calls=model_calls+1 WHERE id=?", (task_id,)
            )


def safe_plan(value):
    value = {
        k: v
        for k, v in value.items()
        if k not in {"model", "model_version", "reasoning_effort", "role_models"}
    }
    plan = Plan.model_validate(value).model_dump()
    for key in ("diagnosis", "expected"):
        plan[key] = note(plan[key])
    plan["unknowns"] = [note(item) for item in plan["unknowns"][:8]]
    return plan


def validate_plan(value, packet):
    plan = safe_plan(value)
    if (
        plan["occurrence_id"] != packet["occurrence_id"]
        or plan["basis"] != packet["basis"]
        or plan["action"] not in packet["allowed_actions"]
        or not set(plan["facts"]) <= set(packet["facts"])
        or len(set(plan["facts"])) != len(plan["facts"])
        or any(len(item) > 512 for item in plan["unknowns"])
        or plan["action"] in {"retry_repair", "wait"}
        and plan["verification"] != "source_verification"
    ):
        raise Conflict("coordination_plan_invalid")
    facts = packet["facts"]
    if facts.get("diagnostic.reason") == "too_many_calls":
        required = {
            key
            for key in (
                "diagnostic.reason",
                "diagnostic.calls",
                "diagnostic.allowed",
                "fault.execution",
            )
            if key in facts
        }
        if plan["category"] != "protocol" or not required <= set(plan["facts"]):
            raise Conflict("coordination_evidence_mismatch")
    return plan


class Coordinator:
    def __init__(self, store, model, repairs):
        self.store, self.model, self.repairs = store, model, repairs
        self.closing = asyncio.Event()

    def state(self):
        with self.store.connection() as db:
            return {**settings(db), "model_ready": self.model.status()["enabled"]}

    def configure(self, *, version, enabled, fallback=None, fallback_authorized=False):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if enabled and not self.model.status()["enabled"]:
                raise Conflict("coordination_model_required")
            if fallback:
                if (
                    set(fallback) != {"provider_id", "model"}
                    or not ModelProviders.resolve(db, fallback)["encrypted_key"]
                ):
                    raise Conflict("coordination_fallback_invalid")
            elif fallback_authorized:
                raise Conflict("coordination_fallback_invalid")
            if not db.execute(
                "UPDATE coordination_configuration SET enabled=?,version=version+1,"
                "enabled_since=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE enabled_since END,"
                "fallback=?,fallback_authorized=? WHERE id=1 AND version=?",
                (
                    enabled,
                    enabled,
                    time.time(),
                    json.dumps(fallback) if fallback else None,
                    fallback_authorized,
                    version,
                ),
            ).rowcount:
                raise Conflict("coordination_configuration_changed")
        return self.state()

    def _basis(self, db, occurrence, config):
        account = db.execute(
            "SELECT active_account_id FROM github_configuration WHERE id=1"
        ).fetchone()
        return recovery.digest(
            [
                source_basis(db, occurrence, self.repairs),
                model_basis(db, config),
                list(account) if account else None,
                config["version"],
            ]
        )

    def claim(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            config = settings(db)
            if not config["enabled"] or self.closing.is_set():
                return None
            rows = db.execute(
                "SELECT o.* FROM system_fault_occurrences o WHERE o.state IN ('pending','blocked') "
                "AND o.occurred>=? AND "
                + SystemFaults._scope(db)
                + " AND NOT EXISTS(SELECT 1 FROM coordination_jobs j WHERE j.occurrence_id=o.id) "
                "AND NOT EXISTS(SELECT 1 FROM system_fault_occurrences newer "
                "WHERE newer.flow=o.flow AND newer.task_id=o.task_id AND "
                "(newer.occurred>o.occurred OR (newer.occurred=o.occurred AND newer.id>o.id))) "
                "ORDER BY o.occurred,o.id LIMIT 100",
                (config["enabled_since"],),
            ).fetchall()
            for occurrence in rows:
                payload = json.loads(occurrence["payload"])
                if payload.get("historical"):
                    continue
                source_error = None
                try:
                    source_budget(db, occurrence, capacity=True)
                except Conflict as exc:
                    source_error = str(exc)
                root = payload.get("root_task_id") or occurrence["task_id"]
                if not db.execute(
                    "SELECT 1 FROM repository_analyses WHERE id=?", (root,)
                ).fetchone():
                    root = "coordination:" + occurrence["id"]
                existing = db.execute("SELECT * FROM recovery_roots WHERE id=?", (root,)).fetchone()
                if existing and existing["action_id"] and existing["lease"] > time.time():
                    continue
                job_id, owner = uid("coordination"), uid("coordinator")
                basis = self._basis(db, occurrence, config)
                action = recovery.claim(
                    db,
                    "coordination",
                    {"id": occurrence["id"], "analysis_id": root},
                    1,
                    basis,
                    owner,
                    time.time() + DEADLINE + 10,
                )
                if not action:
                    continue
                facts = {
                    "fault.code": payload["code"],
                    "fault.category": payload["category"],
                    "fault.execution": payload["execution"],
                }
                facts.update(
                    {
                        "diagnostic." + key: value
                        for key, value in diagnostic(payload.get("diagnostic", {})).items()
                    }
                )
                allowed = ["blocked", "request_authorization"]
                if occurrence["flow"] in {"analysis", "repair", "acceptance"}:
                    allowed.append("wait")
                if (
                    occurrence["flow"] == "repair"
                    and occurrence["code"] in recovery.AUTOMATIC_ERRORS
                    and recovery.state(db)["enabled"]
                ):
                    allowed.append("retry_repair")
                packet = {
                    "occurrence_id": occurrence["id"],
                    "basis": basis,
                    "facts": facts,
                    "allowed_actions": allowed,
                    "root_budget": recovery.snapshot(db, root)["budget"],
                    "receipts": [
                        diagnostic(json.loads(row[0] or "{}"))
                        for row in db.execute(
                            "SELECT receipt FROM recovery_actions WHERE root_id=? AND id<>? "
                            "ORDER BY started DESC LIMIT 3",
                            (root, action),
                        )
                    ],
                }
                now = time.time()
                db.execute(
                    "INSERT INTO coordination_jobs(id,occurrence_id,root_id,state,"
                    "configuration_version,"
                    "basis,owner,lease,action_id,packet,created,updated) "
                    "VALUES(?,?,?,'diagnosing',?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        occurrence["id"],
                        root,
                        config["version"],
                        basis,
                        owner,
                        now + DEADLINE,
                        action,
                        json.dumps(packet),
                        now,
                        now,
                    ),
                )
                transition(
                    db,
                    occurrence,
                    "diagnosing",
                    actor="coordinator",
                    reason="受限诊断已领取",
                    evidence=job_id,
                    source_event_key=job_id + ":claimed",
                )
                job = dict(
                    db.execute("SELECT * FROM coordination_jobs WHERE id=?", (job_id,)).fetchone()
                )
                if source_error:
                    self._finish(db, job, "blocked", error=source_error)
                    recovery.finish(db, action, owner, "source_unavailable")
                    continue
                return job

    def _guard(self, db, job):
        row = db.execute("SELECT * FROM coordination_jobs WHERE id=?", (job["id"],)).fetchone()
        occurrence = db.execute(
            "SELECT o.* FROM system_fault_occurrences o WHERE o.id=? AND "
            + SystemFaults._scope(db),
            (job["occurrence_id"],),
        ).fetchone()
        config = settings(db)
        if (
            not row
            or row["state"] != "diagnosing"
            or row["owner"] != job["owner"]
            or not config["enabled"]
            or config["version"] != job["configuration_version"]
            or row["lease"] <= time.time()
            or not occurrence
            or occurrence["state"] not in ("diagnosing", "pending")
            or self.closing.is_set()
            or job["basis"] != self._basis(db, occurrence, config)
        ):
            raise Conflict("coordination_basis_changed")
        recovery.require(db, job["action_id"], job["owner"])
        source_budget(db, occurrence)
        return occurrence

    def event(self, job, kind, value):
        if kind not in {"request_started", "usage"}:
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind != "usage":
                occurrence = self._guard(db, job)
                source_budget(db, occurrence, reserve=True)
                count = db.execute(
                    "SELECT COUNT(*) FROM recovery_usage u "
                    "JOIN recovery_actions a ON a.id=u.action_id "
                    "WHERE a.root_id=? AND a.flow='coordination' AND u.kind='requests'",
                    (job["root_id"],),
                ).fetchone()[0]
                if count >= MAX_REQUESTS:
                    raise Conflict("coordination_budget_exhausted")
                if count:
                    recovery.observe(db, job["action_id"], job["owner"], "plan_retry")
                db.execute(
                    "UPDATE coordination_jobs SET requests=requests+1 WHERE id=?", (job["id"],)
                )
            recovery.observe(db, job["action_id"], job["owner"], kind, value)

    def _finish(self, db, job, state, *, plan=None, error=None, receipt=None):
        occurrence = db.execute(
            "SELECT * FROM system_fault_occurrences WHERE id=?", (job["occurrence_id"],)
        ).fetchone()
        if occurrence and occurrence["state"] in {"resolved", "dismissed"}:
            state = "completed" if occurrence["state"] == "resolved" else "cancelled"
            error = None
        elif error == "coordination_source_inactive":
            state = "cancelled"
        if not db.execute(
            "UPDATE coordination_jobs SET state=?,version=version+1,plan=COALESCE(?,plan),"
            "error=?,receipt=COALESCE(?,receipt),updated=? WHERE id=? AND owner=? "
            "AND state IN ('diagnosing','waiting')",
            (
                state,
                json.dumps(plan) if plan else None,
                token(error),
                json.dumps(receipt) if receipt else None,
                time.time(),
                job["id"],
                job["owner"],
            ),
        ).rowcount:
            return
        if state in {"blocked", "needs_authorization", "completed"}:
            db.execute(
                "INSERT OR IGNORE INTO coordination_notifications(job_id,version,state,created) "
                "SELECT id,version,state,? FROM coordination_jobs WHERE id=?",
                (time.time(), job["id"]),
            )
        occurrence = db.execute(
            "SELECT * FROM system_fault_occurrences WHERE id=?", (job["occurrence_id"],)
        ).fetchone()
        if state not in {"completed", "cancelled"} and occurrence:
            transition(
                db,
                occurrence,
                "recovering" if state == "waiting" else "blocked",
                actor="coordinator",
                reason=error or "计划已记录，等待原步骤验证",
                evidence=job["id"],
                source_event_key=job["id"] + ":" + state,
            )

    def _reconcile(self, db):
        jobs = db.execute(
            "SELECT * FROM coordination_jobs WHERE state IN ('diagnosing','waiting')"
        ).fetchall()
        for job in jobs:
            occurrence = db.execute(
                "SELECT * FROM system_fault_occurrences WHERE id=?", (job["occurrence_id"],)
            ).fetchone()
            if occurrence["state"] in {"resolved", "dismissed"}:
                self._finish(
                    db, job, "completed" if occurrence["state"] == "resolved" else "cancelled"
                )
            elif job["state"] == "diagnosing" and job["lease"] <= time.time():
                recovery.expire(db, job["root_id"])
                self._finish(db, job, "blocked", error="coordination_interrupted")
            elif job["state"] == "waiting":
                if time.time() >= job["created"] + recovery.MAX_SECONDS:
                    self._finish(db, job, "blocked", error="source_verification_timeout")
                    continue
                try:
                    source_budget(db, occurrence)
                except Conflict as exc:
                    self._finish(db, job, "blocked", error=str(exc))
                    continue
                task = db.execute(
                    "SELECT status FROM repair_tasks WHERE id=?", (occurrence["task_id"],)
                ).fetchone()
                if task and task[0] in {"failed", "interrupted", "cancelled", "stale"}:
                    self._finish(db, job, "blocked", error="source_not_verified")

    def _release(self, job):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if recovery.active(db, job["action_id"], job["owner"], now=0):
                db.execute(
                    "UPDATE recovery_roots SET seconds=seconds+? WHERE id=?",
                    (max(0, time.time() - job["created"]), job["root_id"]),
                )
                recovery.finish(db, job["action_id"], job["owner"], "diagnosed")

    async def tick(self):
        job = await asyncio.to_thread(self.claim)
        if not job:
            return
        packet, plan, error = json.loads(job["packet"]), None, None
        try:
            for attempt in range(MAX_REQUESTS):
                try:
                    with self.store.connection() as db:
                        config = settings(db)
                    fallback = (
                        config["fallback"] if attempt and config["fallback_authorized"] else None
                    )
                    pending = asyncio.create_task(
                        self.model.coordinate(
                            {**packet, "correction": error},
                            lambda k, v: self.event(job, k, v),
                            fallback=fallback,
                        )
                    )
                    try:
                        while not pending.done():
                            await asyncio.wait({pending}, timeout=0.1)
                            with self.store.connection() as db:
                                self._guard(db, job)
                        plan = validate_plan(await pending, packet)
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    break
                except asyncio.CancelledError:
                    raise
                except Conflict as exc:
                    if str(exc) not in {
                        "coordination_plan_invalid",
                        "coordination_evidence_mismatch",
                    }:
                        raise
                    error = str(exc)
                except Exception:
                    error = "coordination_model_unavailable_or_invalid"
            if plan is None:
                # Deterministic fallback cannot grant an action absent from the packet.
                plan = {
                    "occurrence_id": packet["occurrence_id"],
                    "basis": packet["basis"],
                    "category": "protocol"
                    if packet["facts"].get("diagnostic.reason") == "too_many_calls"
                    else "unknown",
                    "diagnosis": "协调模型不可用，保留规则处理结果。",
                    "facts": list(packet["facts"]),
                    "unknowns": ["根因尚未确认"],
                    "action": "retry_repair"
                    if "retry_repair" in packet["allowed_actions"]
                    else "blocked",
                    "expected": "按原流程验证，不能以排队代替通过。",
                    "verification": "source_verification",
                }
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._guard(db, job)
                db.execute(
                    "UPDATE coordination_jobs SET plan=?,error=? WHERE id=?",
                    (json.dumps(plan), error, job["id"]),
                )
                if plan["action"] != "retry_repair":
                    self._finish(
                        db,
                        job,
                        "needs_authorization"
                        if plan["action"] == "request_authorization"
                        else "waiting"
                        if plan["action"] == "wait"
                        else "blocked",
                        plan=plan,
                        error=error
                        or (
                            "authorization_required"
                            if plan["action"] == "request_authorization"
                            else "no_authorized_action"
                            if plan["action"] == "blocked"
                            else None
                        ),
                    )
            self._release(job)
            if plan["action"] == "retry_repair":
                occurrence = packet["occurrence_id"]
                with self.store.connection() as db:
                    task = db.execute(
                        "SELECT t.id,t.version FROM repair_tasks t JOIN "
                        "system_fault_occurrences o ON o.task_id=t.id WHERE o.id=?",
                        (occurrence,),
                    ).fetchone()
                self.repairs.retry(
                    task["id"], task["version"], automatic=True, coordination=(self, job)
                )
        except asyncio.CancelledError:
            with self.store.connection() as db:
                self._finish(db, job, "cancelled", error="coordination_cancelled")
            raise
        except Exception as exc:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._finish(
                    db,
                    job,
                    "blocked",
                    error=str(exc) if isinstance(exc, Conflict) else "coordination_failed",
                )
        finally:
            self._release(job)

    def authorize_retry(self, db, job, task):
        # Runs inside Repairs.retry's transaction, after diagnostic ownership was released.
        config = settings(db)
        saved = db.execute("SELECT * FROM coordination_jobs WHERE id=?", (job["id"],)).fetchone()
        occurrence = db.execute(
            "SELECT o.* FROM system_fault_occurrences o WHERE o.id=? AND "
            + SystemFaults._scope(db),
            (job["occurrence_id"],),
        ).fetchone()
        if (
            not saved
            or saved["state"] != "diagnosing"
            or saved["owner"] != job["owner"]
            or not config["enabled"]
            or config["version"] != job["configuration_version"]
            or job["lease"] <= time.time()
            or not occurrence
            or occurrence["state"] != "diagnosing"
            or self.closing.is_set()
            or self._basis(db, occurrence, config) != job["basis"]
            or task["id"] != occurrence["task_id"]
        ):
            raise Conflict("coordination_basis_changed")
        plan = validate_plan(json.loads(saved["plan"]), json.loads(saved["packet"]))
        if plan["action"] != "retry_repair":
            raise Conflict("coordination_action_forbidden")
        self._finish(db, job, "waiting", receipt={"state": "queued", "task_id": task["id"]})
        return config["version"]

    def list(self, fault_id=None):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT j.* FROM coordination_jobs j JOIN system_fault_occurrences o "
                "ON o.id=j.occurrence_id WHERE "
                + SystemFaults._scope(db)
                + (" AND o.fault_id=?" if fault_id else "")
                + " ORDER BY j.updated DESC LIMIT 100",
                (fault_id,) if fault_id else (),
            ).fetchall()
            return [
                {
                    key: json.loads(row[key])
                    if key in {"plan", "receipt"} and row[key]
                    else row[key]
                    for key in (
                        "id",
                        "occurrence_id",
                        "root_id",
                        "state",
                        "version",
                        "plan",
                        "error",
                        "requests",
                        "receipt",
                        "updated",
                    )
                }
                | {"budget": recovery.snapshot(db, row["root_id"])["budget"]}
                for row in rows
            ]

    def notifications(self):
        with self.store.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT n.id,n.state,n.created,j.id job_id,j.error,o.fault_id "
                    "FROM coordination_notifications n "
                    "JOIN coordination_jobs j ON j.id=n.job_id JOIN system_fault_occurrences o "
                    "ON o.id=j.occurrence_id WHERE n.seen=0 AND "
                    + SystemFaults._scope(db)
                    + " ORDER BY n.id DESC LIMIT 100"
                )
            ]

    def acknowledge(self, notification_id):
        with self.store.connection() as db:
            if not any(row["id"] == notification_id for row in self.notifications()):
                raise Missing("Notification not found.")
            db.execute(
                "UPDATE coordination_notifications SET seen=1 WHERE id=?", (notification_id,)
            )
        return {"ok": True}

    async def run(self):
        while not self.closing.is_set():
            try:
                await self.tick()
            except Exception:
                LOG.warning("Coordination worker unavailable", exc_info=False)
            try:
                await asyncio.wait_for(self.closing.wait(), timeout=1)
            except TimeoutError:
                pass
