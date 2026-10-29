"""Opt-in self-maintenance. PR receipts never resolve the originating fault."""

import ast
import asyncio
import json
import logging
import re
import textwrap
import threading
import time

from . import recovery
from .coordination import source_basis, source_budget
from .diagnosis import redact_values
from .fault_contract import diagnostic, note, token
from .maintenance_model import MaintenanceDiagnosis
from .maintenance_target import LOGIN, REPOSITORY, MaintenanceTarget
from .maintenance_workspace import approved_artifacts, create_workspace
from .repair_model import RepairReview, repair_failure_code
from .store import Conflict, Missing, uid
from .system_faults import SystemFaults, transition

LOG = logging.getLogger(__name__)
MAX_REQUESTS = 32
DEADLINE = 1800
EXCLUDED = {"model_timeout", "external_service", "target_test", "review_rejection"}
METADATA = {"model", "model_version", "role_models", "reasoning_effort"}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS maintenance_configuration (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1, enabled_since REAL, binding TEXT
        );
        INSERT OR IGNORE INTO maintenance_configuration(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS maintenance_tasks (
            id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL UNIQUE
                REFERENCES system_fault_occurrences(id), root_id TEXT NOT NULL,
            state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            configuration_version INTEGER NOT NULL, basis TEXT NOT NULL, binding TEXT NOT NULL,
            owner TEXT NOT NULL, lease REAL NOT NULL, action_id TEXT NOT NULL,
            code TEXT NOT NULL, packet TEXT, diagnosis TEXT, fingerprint TEXT,
            base TEXT, default_branch TEXT, branch TEXT, issue TEXT, remote TEXT,
            artifacts TEXT, error TEXT, requests INTEGER NOT NULL DEFAULT 0,
            publication_retries INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
                REFERENCES maintenance_tasks(id), kind TEXT NOT NULL,
            value TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_remote_operations (
            task_id TEXT NOT NULL REFERENCES maintenance_tasks(id), operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
            PRIMARY KEY(task_id,operation)
        );
    """)


def settings(db):
    row = dict(db.execute("SELECT * FROM maintenance_configuration WHERE id=1").fetchone())
    return {
        **row,
        "enabled": bool(row["enabled"]),
        "binding": json.loads(row["binding"]) if row["binding"] else None,
        "repository": REPOSITORY,
        "account": LOGIN,
        "merge": "owner_only",
        "max_requests": MAX_REQUESTS,
        "max_seconds": DEADLINE,
    }


def diagnosis_for(value, packet):
    result = MaintenanceDiagnosis.model_validate(
        {k: v for k, v in value.items() if k not in METADATA}
    ).model_dump()
    if not set(result["facts"]) <= set(packet["facts"]) or len(set(result["facts"])) != len(
        result["facts"]
    ):
        raise Conflict("maintenance_evidence_invalid")
    if result["classification"] != "internal_defect":
        return result
    source = packet["sources"].get(result["path"], "")
    offset = packet.get("source_starts", {}).get(result["path"], 1)
    try:
        symbols = [
            node
            for node in ast.walk(ast.parse(textwrap.dedent(source)))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == result["symbol"]
        ]
    except SyntaxError:
        symbols = []
    start, end = result["start_line"], result["end_line"]
    if (
        not result["path"].startswith("repowatch/")
        or not result["path"].endswith(".py")
        or not re.search(r"[\u4e00-\u9fff]", result["title"])
        or not {"fault.code", "fault.execution"} <= set(result["facts"])
        or not offset <= start <= end < offset + len(source.splitlines())
        or not result["quote"].strip()
        or "\n".join(source.splitlines()[start - offset : end - offset + 1]) != result["quote"]
        or not any(
            node.lineno + offset - 1 <= start <= end <= node.end_lineno + offset - 1
            for node in symbols
        )
    ):
        raise Conflict("maintenance_internal_evidence_required")
    # The quote is screened source; free model prose is not a credential channel.
    for key in ("title", "root_cause", "expected", "observed"):
        result[key] = note(result[key])
    return result


class Maintenance:
    def __init__(self, execution, model, github, repairs, *, target=None, workspace_factory=None):
        self.execution, self.store, self.model = execution, execution.store, model
        self.github, self.repairs = github, repairs
        self.target = target or MaintenanceTarget(github)
        self.workspace_factory = workspace_factory or create_workspace
        self.closing = threading.Event()

    def state(self):
        with self.store.connection() as db:
            return {**settings(db), "model_ready": self.model.status()["enabled"]}

    def configure(self, *, version, enabled):
        binding = None
        if enabled:
            if not self.model.status()["enabled"]:
                raise Conflict("maintenance_model_required")
            with self.store.connection() as db:
                account = db.execute(
                    "SELECT a.id,a.version FROM github_accounts a JOIN "
                    "github_configuration c ON c.active_account_id=a.id WHERE c.id=1"
                ).fetchone()
            if not account:
                raise Conflict("maintenance_identity_required")
            binding = {"account_id": account["id"], "account_version": account["version"]}
            _, _, metadata = self.target.authority(**binding)
            binding["repository_id"] = metadata["id"]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "UPDATE maintenance_configuration SET enabled=?,version=version+1,"
                "enabled_since=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE enabled_since END,"
                "binding=? WHERE id=1 AND version=?",
                (enabled, enabled, time.time(), json.dumps(binding) if binding else None, version),
            ).rowcount:
                raise Conflict("maintenance_configuration_changed")
        return self.state()

    def basis(self, db, occurrence):
        roles = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM agent_models WHERE role IN ('repair_agent','independent_reviewer') "
                "ORDER BY role"
            )
        ]
        from .model_providers import ModelProviders

        effective = ModelProviders.role_rows(db)
        model = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
        return recovery.digest(
            [
                source_basis(db, occurrence, self.repairs),
                roles,
                dict(model) if model else None,
                {role: effective.get(role) for role in ("repair_agent", "independent_reviewer")},
            ]
        )

    def claim(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.store.draining(db):
                return None
            for old in db.execute(
                "SELECT * FROM maintenance_tasks WHERE state='running' AND lease<=?", (time.time(),)
            ).fetchall():
                recovery.expire(db, old["root_id"])
                self.finish(db, old, "blocked", "maintenance_interrupted")
            config = settings(db)
            if not config["enabled"] or self.closing.is_set():
                return None
            rows = db.execute(
                "SELECT o.* FROM system_fault_occurrences o WHERE "
                "o.state IN ('pending','blocked') AND o.occurred>=? AND "
                + SystemFaults._scope(db)
                + " AND NOT EXISTS(SELECT 1 FROM maintenance_tasks t WHERE t.occurrence_id=o.id) "
                "AND NOT EXISTS(SELECT 1 FROM system_fault_occurrences n WHERE n.flow=o.flow "
                "AND n.task_id=o.task_id AND (n.occurred>o.occurred OR "
                "(n.occurred=o.occurred AND n.id>o.id))) ORDER BY o.occurred LIMIT 50",
                (config["enabled_since"],),
            ).fetchall()
            for occurrence in rows:
                payload = json.loads(occurrence["payload"])
                if payload.get("historical"):
                    continue
                root = payload.get("root_task_id") or occurrence["task_id"]
                if not db.execute(
                    "SELECT 1 FROM repository_analyses WHERE id=?", (root,)
                ).fetchone():
                    root = "maintenance:" + occurrence["id"]
                task_id, owner = uid("maintenance"), uid("maintainer")
                basis = self.basis(db, occurrence)
                action = recovery.claim(
                    db,
                    "maintenance",
                    {"id": task_id, "analysis_id": root},
                    1,
                    basis,
                    owner,
                    time.time() + DEADLINE + 10,
                )
                if not action:
                    continue
                now = time.time()
                db.execute(
                    "INSERT INTO maintenance_tasks(id,occurrence_id,root_id,state,"
                    "configuration_version,basis,binding,owner,lease,action_id,"
                    "code,created,updated) "
                    "VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        occurrence["id"],
                        root,
                        config["version"],
                        basis,
                        json.dumps(config["binding"]),
                        owner,
                        now + DEADLINE,
                        action,
                        payload["code"],
                        now,
                        now,
                    ),
                )
                task = dict(
                    db.execute("SELECT * FROM maintenance_tasks WHERE id=?", (task_id,)).fetchone()
                )
                if payload["category"] in EXCLUDED:
                    self.finish(db, task, "not_internal", "maintenance_non_internal_category")
                    recovery.finish(db, action, owner, "not_internal")
                    continue
                try:
                    source_budget(db, occurrence, capacity=True, maintenance=True)
                except Conflict as exc:
                    self.finish(db, task, "blocked", str(exc))
                    recovery.finish(db, action, owner, "blocked", str(exc))
                    continue
                transition(
                    db,
                    occurrence,
                    "diagnosing",
                    actor="maintenance",
                    reason="内部缺陷证据核对",
                    evidence=task_id,
                    source_event_key=task_id + ":claimed",
                )
                task["binding"] = json.loads(task["binding"])
                task["facts"] = {
                    "fault.code": payload["code"],
                    "fault.execution": payload["execution"],
                    "fault.category": payload["category"],
                    **{
                        "diagnostic." + key: value
                        for key, value in diagnostic(payload.get("diagnostic", {})).items()
                    },
                }
                return task

    def guard(self, task):
        with self.store.connection() as db:
            return self._guard(db, task)

    def _guard(self, db, task):
        row = db.execute("SELECT * FROM maintenance_tasks WHERE id=?", (task["id"],)).fetchone()
        config = settings(db)
        occurrence = db.execute(
            "SELECT o.* FROM system_fault_occurrences o WHERE o.id=? AND "
            + SystemFaults._scope(db),
            (task["occurrence_id"],),
        ).fetchone()
        if (
            not row
            or row["state"] != "running"
            or row["owner"] != task["owner"]
            or row["lease"] <= time.time()
            or self.closing.is_set()
            or not config["enabled"]
            or config["version"] != task["configuration_version"]
            or config["binding"] != task["binding"]
            or not occurrence
            or occurrence["state"] in {"resolved", "dismissed"}
            or self.basis(db, occurrence) != task["basis"]
        ):
            raise Conflict("maintenance_basis_changed")
        self.github.row(db, task["binding"]["account_id"], task["binding"]["account_version"])
        active = db.execute(
            "SELECT active_account_id FROM github_configuration WHERE id=1"
        ).fetchone()[0]
        if active != task["binding"]["account_id"]:
            raise Conflict("maintenance_identity_required")
        recovery.require(db, task["action_id"], task["owner"])
        source_budget(db, occurrence, maintenance=True)
        return occurrence

    def save(self, task, **fields):
        allowed = {
            "packet",
            "diagnosis",
            "fingerprint",
            "base",
            "default_branch",
            "branch",
            "issue",
            "remote",
            "artifacts",
        }
        if not fields.keys() <= allowed:
            raise ValueError("invalid maintenance field")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db, task)
            db.execute(
                "UPDATE maintenance_tasks SET "
                + ",".join(key + "=?" for key in fields)
                + ",updated=? WHERE id=?",
                (
                    *[
                        json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (dict, list))
                        else value
                        for value in fields.values()
                    ],
                    time.time(),
                    task["id"],
                ),
            )
        task.update(fields)

    def event(self, task, kind, value):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind != "usage":
                occurrence = self._guard(db, task)
                if kind == "request_started":
                    count = db.execute(
                        "SELECT requests FROM maintenance_tasks WHERE id=?", (task["id"],)
                    ).fetchone()[0]
                    if count >= MAX_REQUESTS:
                        raise Conflict("maintenance_budget_exhausted")
                    source_budget(db, occurrence, reserve=True, maintenance=True)
                    db.execute(
                        "UPDATE maintenance_tasks SET requests=requests+1 WHERE id=?", (task["id"],)
                    )
            recovery.observe(db, task["action_id"], task["owner"], kind, value)
            if kind != "usage":
                # Tool output, provider errors and source bodies never enter event logs.
                db.execute(
                    "INSERT INTO maintenance_events(task_id,kind,value,created) VALUES(?,?,?,?)",
                    (
                        task["id"],
                        token(kind) or "progress",
                        token(value) or "recorded",
                        time.time(),
                    ),
                )

    def finish(self, db, task, state, error=None):
        db.execute(
            "UPDATE maintenance_tasks SET state=?,error=?,version=version+1,updated=? "
            "WHERE id=? AND state='running' AND owner=?",
            (state, token(error), time.time(), task["id"], task["owner"]),
        )
        occurrence = db.execute(
            "SELECT * FROM system_fault_occurrences WHERE id=?", (task["occurrence_id"],)
        ).fetchone()
        if occurrence and occurrence["state"] not in {"resolved", "dismissed"}:
            transition(
                db,
                occurrence,
                "blocked",
                actor="maintenance",
                reason=error
                or (
                    "证据未确认内部缺陷，不创建维护补丁"
                    if state == "not_internal"
                    else "维护 PR 待所有者处理，原步骤尚未验证"
                ),
                evidence=task["id"],
                source_event_key=task["id"] + ":" + state,
            )

    async def guarded(self, task, awaitable):
        pending = asyncio.create_task(awaitable)
        try:
            while not pending.done():
                await asyncio.wait({pending}, timeout=0.1)
                self.guard(task)
            self.guard(task)
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def tick(self):
        task = await asyncio.to_thread(self.claim)
        if not task:
            return
        workspace, state, error = None, "blocked", None

        def check():
            return self.guard(task)

        def event(kind, value):
            return self.event(task, kind, value)

        try:
            packet = await asyncio.to_thread(
                self.target.evidence, task["binding"], task["facts"], check
            )
            self.save(
                task,
                packet=packet,
                base=packet["base"],
                default_branch=packet["default_branch"],
                branch="feature/" + task["id"].replace("_", "-"),
            )
            result = await self.guarded(task, self.model.classify_maintenance(packet, event))
            diagnosis = diagnosis_for(result, packet)
            self.save(task, diagnosis=diagnosis)
            if diagnosis["classification"] != "internal_defect":
                state = "not_internal"
                return
            fingerprint = recovery.digest(
                [
                    REPOSITORY,
                    task["code"],
                    diagnosis["path"],
                    diagnosis["symbol"],
                    diagnosis["invariant"],
                ]
            )
            self.save(task, fingerprint=fingerprint)
            issue, related = await asyncio.to_thread(
                self.target.ensure_issue, task, diagnosis, check
            )
            self.save(task, issue=issue)
            if related:
                self.save(task, remote=related)
                state = "waiting_deployment" if related["merged"] else "linked_pr"
                return
            # Authority and Issue must be confirmed before any local source checkout.
            auth, _, _ = await asyncio.to_thread(self.target.authority, **task["binding"])
            check()
            workspace = await asyncio.to_thread(
                self.workspace_factory, self.execution, task, auth, check
            )
            prepare = workspace.prepare

            def authorized_prepare(*args, **kwargs):
                check()
                self.target.authority(**task["binding"])
                check()
                return prepare(*args, **kwargs)

            workspace.prepare = authorized_prepare
            context = {
                "system_maintenance": True,
                "maintenance_task_id": task["id"],
                "finding": {
                    "id": task["id"],
                    "title": diagnosis["title"],
                    "description": diagnosis["root_cause"],
                    "evidence": diagnosis,
                    "path": diagnosis["path"],
                    "line": diagnosis["start_line"],
                },
                "confirmation": {
                    "authorization": "maintenance_policy",
                    "repository": REPOSITORY,
                    "configuration_version": task["configuration_version"],
                    "issue": issue,
                },
                "fault": task["facts"],
            }
            result = await self.guarded(
                task, self.model.repair(context, event, workspace=workspace)
            )
            review = {key: value for key, value in result.items() if key not in METADATA}
            RepairReview.model_validate(
                {key: review[key] for key in RepairReview.model_fields if key in review}
            )
            artifacts = await asyncio.to_thread(approved_artifacts, workspace, review)
            self.save(task, artifacts=artifacts)
            task["approved_digest"] = artifacts["approved_digest"]
            remote = await asyncio.to_thread(
                self.target.publish,
                task,
                artifacts["patch"],
                artifacts["verification"],
                review,
                check,
            )
            self.save(task, remote=remote)
            state = "pr_created"
        except asyncio.CancelledError:
            error = "maintenance_cancelled"
            raise
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, Conflict)
                else (repair_failure_code(exc) if workspace else "maintenance_failed")
            )
        finally:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if workspace:
                    # Preserve failed verification/review history too, without weakening gates.
                    evidence = {
                        "patch": workspace.patch.model_dump() if workspace.patch else None,
                        "diff": workspace.diff,
                        "verification": workspace.verification,
                        "review": workspace.review,
                        "review_history": workspace.review_history,
                        "verification_history": workspace.verification_history,
                    }
                    db.execute(
                        "UPDATE maintenance_tasks SET artifacts=COALESCE(artifacts,?) WHERE id=?",
                        (json.dumps(redact_values(evidence), ensure_ascii=False), task["id"]),
                    )
                self.finish(db, task, state, error)
            recovery.release(
                self.store,
                task["action_id"],
                task["owner"],
                "maintenance_tasks",
                task["id"],
                status="state",
            )

    def scoped_task(self, db, task_id, version):
        row = db.execute(
            "SELECT t.* FROM maintenance_tasks t JOIN system_fault_occurrences o "
            "ON o.id=t.occurrence_id WHERE t.id=? AND " + SystemFaults._scope(db),
            (task_id,),
        ).fetchone()
        if not row:
            raise Missing("Maintenance task not found.")
        task = dict(row)
        if task["version"] != version:
            raise Conflict("maintenance_task_changed")
        for key in ("binding", "diagnosis", "issue", "remote", "artifacts"):
            task[key] = json.loads(task[key]) if task[key] else None
        return task

    def reconcile(self, task_id, version):
        with self.store.connection() as db:
            task = self.scoped_task(db, task_id, version)
        request, _, _, _ = self.target.session(task["binding"], lambda: None)
        remote = task["remote"]
        if not remote:
            # A lost Issue response can be reconciled without replaying its POST.
            with self.store.connection() as db:
                pending = db.execute(
                    "SELECT * FROM maintenance_remote_operations WHERE "
                    "task_id=? AND operation='issue' AND state='pending'",
                    (task_id,),
                ).fetchone()
            if not pending or not task["fingerprint"]:
                raise Conflict("maintenance_receipt_unavailable")
            matches = [
                item
                for item in self.target.items(request, "issues")
                if "pull_request" not in item and self.target.matches(item, task["fingerprint"])
            ]
            if len(matches) != 1:
                raise Conflict("maintenance_publication_uncertain")
            from .github_contributions import remote_link

            issue = remote_link(matches[0], REPOSITORY, "issues")
            self.target._save_operation(
                task_id, "issue", pending["payload_hash"], "complete", issue
            )
            with self.store.connection() as db:
                db.execute(
                    "UPDATE maintenance_tasks SET issue=?,version=version+1,updated=? "
                    "WHERE id=? AND version=? AND state='blocked'",
                    (json.dumps(issue), time.time(), task_id, version),
                )
            return {"issue": issue, "state": "blocked"}
        item = request("GET", f"/repos/{REPOSITORY}/pulls/{remote['number']}")
        if not self.target.root_match(
            item, task, task["diagnosis"]
        ) or f"#{task['issue']['number']}" not in (item.get("body") or ""):
            raise Conflict("maintenance_pr_changed")
        state = "waiting_deployment" if item.get("merged_at") else task["state"]
        with self.store.connection() as db:
            db.execute(
                "UPDATE maintenance_tasks SET state=?,version=version+1,updated=? "
                "WHERE id=? AND version=? "
                "AND state IN ('pr_created','linked_pr','waiting_deployment')",
                (state, time.time(), task_id, version),
            )
        return {"state": state, "remote": remote}

    def retry_publication(self, task_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.scoped_task(db, task_id, version)
            artifacts = task["artifacts"]
            if (
                task["state"] != "blocked"
                or not task["issue"]
                or not artifacts
                or not artifacts.get("approved_digest")
                or task["remote"]
                or task["publication_retries"] >= 2
                or time.time() >= task["lease"]
                or not artifacts.get("review", {}).get("approved")
                or not artifacts.get("verification", {}).get("passed")
            ):
                raise Conflict("maintenance_publication_retry_unavailable")
            action = recovery.claim(
                db,
                "maintenance",
                {"id": task_id, "analysis_id": task["root_id"]},
                task["publication_retries"] + 2,
                task["basis"],
                task["owner"],
                task["lease"] + 10,
            )
            if not action:
                raise Conflict("maintenance_root_busy")
            db.execute(
                "UPDATE maintenance_tasks SET state='running',version=version+1,"
                "publication_retries=publication_retries+1,action_id=? WHERE id=?",
                (action, task_id),
            )
            task["action_id"] = action
            self._guard(db, task)
        state, error = "blocked", None
        try:
            task["approved_digest"] = artifacts["approved_digest"]
            remote = self.target.publish(
                task,
                artifacts["patch"],
                artifacts["verification"],
                artifacts["review"],
                lambda: self.guard(task),
            )
            self.save(task, remote=remote)
            state = "pr_created"
        except Exception as exc:
            error = str(exc) if isinstance(exc, Conflict) else "maintenance_publication_failed"
        finally:
            with self.store.connection() as db:
                self.finish(db, task, state, error)
            recovery.release(
                self.store,
                task["action_id"],
                task["owner"],
                "maintenance_tasks",
                task_id,
                status="state",
            )
        return {"state": state, "error": error}

    def list(self, fault_id=None):
        with self.store.connection() as db:
            config = settings(db)
            rows = db.execute(
                "SELECT t.* FROM maintenance_tasks t JOIN system_fault_occurrences o "
                "ON o.id=t.occurrence_id WHERE "
                + SystemFaults._scope(db)
                + (" AND o.fault_id=?" if fault_id else "")
                + " ORDER BY t.created DESC LIMIT 50",
                (fault_id,) if fault_id else (),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for key in ("binding", "packet", "diagnosis", "issue", "remote", "artifacts"):
                    item[key] = json.loads(item[key]) if item[key] else None
                item["budget"] = recovery.snapshot(db, row["root_id"])["budget"]
                artifacts = item["artifacts"] or {}
                item["can_retry_publication"] = bool(
                    row["state"] == "blocked" and row["lease"] > time.time()
                    and row["publication_retries"] < 2 and row["issue"] and not row["remote"]
                    and artifacts.get("approved_digest") and config["enabled"]
                    and config["version"] == row["configuration_version"])
                item["can_reconcile"] = bool(row["remote"] or db.execute(
                    "SELECT 1 FROM maintenance_remote_operations WHERE task_id=? "
                    "AND operation='issue' AND state='pending'", (row["id"],)).fetchone())
                item["events"] = [
                    dict(e)
                    for e in db.execute(
                        "SELECT kind,value,created FROM "
                        "maintenance_events WHERE task_id=? ORDER BY id DESC LIMIT 30",
                        (row["id"],),
                    )
                ]
                item.pop("owner", None)
                item.pop("packet", None)
                result.append(item)
            return redact_values(result)

    async def run(self):
        while not self.closing.is_set():
            try:
                await self.tick()
            except Exception:
                LOG.error("Maintenance worker failed; no automatic side-effect replay")
            for _ in range(20):
                if self.closing.is_set():
                    break
                await asyncio.sleep(0.1)
