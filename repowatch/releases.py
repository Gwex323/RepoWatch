"""Owner-authorized release transactions and source-owned post-release recovery."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from . import recovery
from .release_manifest import Manifest, absolute
from .release_runtime import NativeRuntime, Supervisor, local_authorization, port_free
from .release_state import (
    Journal,
    atomic_json,
    compatible,
    existing_database,
    idle,
    locked,
    read_json,
    schema,
)
from .store import Conflict


def data_binding(db, template=None):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    excluded = {"sessions", "release_control", "release_events", "release_resumptions",
                "system_fault_ingestion"}
    append_only = {"system_faults", "system_fault_occurrences", "system_fault_history",
                   "system_fault_outbox"}
    names = set(template) if template is not None else {
        name for name in tables if name not in excluded
        and not name.startswith("sqlite_")}
    result = {}
    for name in sorted(names):
        if name not in tables:
            raise Conflict("release_original_table_missing")
        quoted = '"' + name.replace('"', '""') + '"'
        columns = template[name]["columns"] if template is not None else [
            row["name"] for row in db.execute("PRAGMA table_info(" + quoted + ")")]
        if template is None and name == "system_fault_occurrences":
            columns = [item for item in columns if item not in {"state", "version"}]
        if template is None and name == "system_fault_outbox":
            columns = [item for item in columns if item != "consumed"]
        selected = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
        maximum = template[name].get("max_rowid") if template is not None else None
        if template is None and name in append_only:
            maximum = db.execute("SELECT COALESCE(MAX(rowid),0) FROM " + quoted).fetchone()[0]
        condition = " WHERE rowid<=?" if maximum is not None else ""
        rows = sorted(recovery.digest(dict(row)) for row in
                      db.execute("SELECT " + selected + " FROM " + quoted + condition,
                                 (maximum,) if maximum is not None else ()))
        result[name] = {"columns": columns, "digest": recovery.digest(rows)}
        if maximum is not None:
            result[name]["max_rowid"] = maximum
    return result


class ReleaseManager:
    def __init__(self, authority, *, runtime_factory=NativeRuntime, migration_probe=None):
        self.authority = authority
        self.runtime_factory = runtime_factory
        self.migration_probe = migration_probe or self.probe

    @staticmethod
    def probe(manifest, manifest_path, database):
        import os
        import subprocess

        command = [manifest.python, "-I", str(Path(manifest.code_root) / "scripts/managed.py"),
                   "probe", "--manifest", str(manifest_path), "--database", str(database)]
        result = subprocess.run(command, capture_output=True, timeout=60, check=False,
                                env={k: v for k, v in os.environ.items()
                                     if not k.startswith(("PYTHON", "REPOWATCH_", "LANGFUSE_"))})
        if result.returncode:
            raise Conflict("release_migration_probe_failed")

    @staticmethod
    def _complete(manifest, state):
        root = Path(manifest.state_root)
        pending = read_json(root / "pending.json")
        atomic_json(root / "pending.json", {**pending, "stage": "completed", "result": state})
        with existing_database(manifest.database) as db:
            db.execute("UPDATE release_control SET draining=NULL WHERE id=1 AND draining=?",
                       (manifest.id,))
        (root / "pending.json").unlink()

    def deploy(self, path, *, drain_seconds=30):
        path = absolute(path)
        manifest = Manifest.load(path)
        root = absolute(manifest.state_root)
        root.mkdir(parents=True, exist_ok=True)
        journal = Journal(root)
        with locked(root / "action.lock"):
            if read_json(root / "pending.json"):
                raise Conflict("release_interrupted_requires_reconciliation")
            self.authority.require("deploy", manifest.id, manifest.binding)
            self.authority.merged(manifest.pr, manifest.commit, manifest.binding)
            manifest.validate_files()
            selection = read_json(root / "selection.json", {})
            old_path = selection.get("current")
            previous = Manifest.load(old_path) if old_path else None
            if manifest.previous != old_path:
                raise Conflict("release_previous_manifest_changed")
            if previous and (previous.database != manifest.database
                             or previous.database_id != manifest.database_id
                             or previous.port != manifest.port or previous.state_root != str(root)):
                raise Conflict("release_workbench_binding_changed")
            if previous:
                old_values = previous.validate_files()
                new_values = manifest.validate_files()
                if (old_values.get("REPOWATCH_LANGFUSE_ENABLED") == "1"
                        and new_values.get("REPOWATCH_LANGFUSE_ENABLED") != "1"):
                    raise Conflict("release_monitoring_preservation_required")
            if not previous and not port_free(manifest.port):
                raise Conflict("release_unmanaged_port")
            pending = {"candidate": str(path), "previous": old_path, "stage": "draining"}
            atomic_json(root / "pending.json", pending)
            switched = False
            try:
                with existing_database(manifest.database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT draining FROM release_control WHERE id=1").fetchone()[0]:
                        raise Conflict("release_already_draining")
                    db.execute("UPDATE release_control SET draining=? WHERE id=1", (manifest.id,))
                deadline = time.monotonic() + drain_seconds
                while True:
                    with existing_database(manifest.database) as db:
                        ready = idle(db)
                    if ready:
                        break
                    if time.monotonic() >= deadline:
                        raise Conflict("release_active_tasks")
                    time.sleep(0.25)
                backup = root / "backups" / (manifest.id + "-" + str(time.time_ns()) + ".db")
                backup.parent.mkdir(parents=True, exist_ok=True)
                with existing_database(manifest.database) as db:
                    before_schema, before_data = schema(db), data_binding(db)
                    with closing(sqlite3.connect(backup)) as destination:
                        db.backup(destination)
                    with existing_database(backup) as saved:
                        if (saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                                or data_binding(saved, before_data) != before_data):
                            raise Conflict("release_backup_failed")
                probe_path = backup.with_suffix(".probe.db")
                with (existing_database(backup) as source,
                      closing(sqlite3.connect(probe_path)) as target):
                    source.backup(target)
                self.migration_probe(manifest, path, probe_path)
                with existing_database(probe_path) as probe:
                    if (not compatible(before_schema, schema(probe))
                            or data_binding(probe, before_data) != before_data):
                        raise Conflict("release_migration_incompatible")
                self.authority.require("deploy", manifest.id, manifest.binding)
                self.authority.merged(manifest.pr, manifest.commit, manifest.binding)
                manifest.validate_files()
                pending.update(stage="switching", data=before_data, schema=before_schema,
                               backup=str(backup))
                atomic_json(root / "pending.json", pending)
                with existing_database(manifest.database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    if not idle(db) or data_binding(db, before_data) != before_data:
                        raise Conflict("release_drain_changed")
                    if previous:
                        self.runtime_factory(previous, old_path).stop()
                atomic_json(root / "selection.json", {"current": str(path), "previous": old_path})
                switched = True
                runtime = self.runtime_factory(manifest, path)
                runtime.start()
                runtime.wait()
                with existing_database(manifest.database) as db:
                    if data_binding(db, before_data) != before_data:
                        raise Conflict("release_original_data_changed")
                journal.event(manifest.id, "deployed", commit=manifest.commit,
                              event_key=manifest.id + ":deployed",
                              maintenance_task=manifest.maintenance_task)
                Supervisor(root).status(manifest, "healthy")
                self._complete(manifest, "healthy")
                return {"id": manifest.id, "state": "healthy", "commit": manifest.commit,
                        "backup": str(backup)}
            except Exception as exc:
                completed = read_json(root / "pending.json", {}).get("stage") == "completed"
                reason = ("release_completion_requires_reconciliation" if completed else
                          str(exc) if isinstance(exc, Conflict) else "release_deployment_failed")
                journal.event(manifest.id, "failed", reason, commit=manifest.commit,
                              mismatches=getattr(exc, "mismatches", {}))
                if completed:
                    Supervisor(root).status(manifest, "blocked", reason)
                elif switched:
                    try:
                        self._rollback(manifest, path, previous, old_path, journal)
                        self._complete(manifest, "rolled_back")
                    except Exception as rollback_error:
                        Supervisor(root).status(
                            manifest, "blocked",
                            str(rollback_error) if isinstance(rollback_error, Conflict)
                            else "release_rollback_failed")
                else:
                    self._complete(manifest, "prepared_cancelled")
                raise Conflict(reason) from None
            finally:
                if not read_json(root / "pending.json"):
                    with existing_database(manifest.database) as db:
                        db.execute("UPDATE release_control SET draining=NULL "
                                   "WHERE id=1 AND draining=?", (manifest.id,))

    def _rollback(self, failed, failed_path, previous, old_path, journal):
        if not previous:
            raise Conflict("release_previous_manifest_required")
        previous.validate_files(rollback=True)
        local_authorization(previous)
        self.runtime_factory(failed, failed_path).stop()
        root = Path(failed.state_root)
        atomic_json(root / "selection.json",
                    {"current": old_path, "previous": previous.previous})
        runtime = self.runtime_factory(previous, old_path)
        runtime.start()
        runtime.wait()
        journal.event(failed.id, "rollback", "release_code_rollback",
                      event_key=failed.id + ":rollback",
                      commit=previous.commit, previous=previous.id)
        Supervisor(root).status(previous, "healthy")

    def rollback(self, current_path):
        current = Manifest.load(current_path)
        root = Path(current.state_root)
        with locked(root / "action.lock"):
            if read_json(root / "pending.json") or read_json(root / "restart.json"):
                raise Conflict("release_interrupted_requires_reconciliation")
            selection = read_json(root / "selection.json", {})
            if selection.get("current") != str(current_path) or not current.previous:
                raise Conflict("release_previous_manifest_changed")
            previous = Manifest.load(current.previous)
            self.authority.require("deploy", previous.id, previous.binding)
            self.authority.merged(previous.pr, previous.commit, previous.binding)
            with existing_database(current.database) as db:
                db.execute("BEGIN IMMEDIATE")
                if not idle(db) or db.execute(
                        "SELECT draining FROM release_control WHERE id=1").fetchone()[0]:
                    raise Conflict("release_active_tasks")
                atomic_json(root / "pending.json",
                            {"candidate": str(current_path), "previous": current.previous,
                             "stage": "rolling_back"})
                db.execute("UPDATE release_control SET draining=? WHERE id=1", (current.id,))
            try:
                self._rollback(current, current_path, previous, current.previous, Journal(root))
                self._complete(current, "rolled_back")
            finally:
                if not read_json(root / "pending.json"):
                    with existing_database(current.database) as db:
                        db.execute("UPDATE release_control SET draining=NULL "
                                   "WHERE id=1 AND draining=?", (current.id,))
            return {"state": "rolled_back", "id": previous.id}

    def _restore_previous(self, previous, path):
        runtime = self.runtime_factory(previous, path)
        try:
            runtime.health()
        except Exception:
            previous.validate_files(rollback=True)
            local_authorization(previous)
            with existing_database(previous.database) as db:
                db.execute("BEGIN IMMEDIATE")
                if not idle(db):
                    raise Conflict("release_active_tasks")
                runtime.stop()
            runtime.start()
            runtime.wait()

    def reconcile(self, path):
        manifest = Manifest.load(path)
        root = Path(manifest.state_root)
        with locked(root / "action.lock"):
            pending = read_json(root / "pending.json")
            if not pending and read_json(root / "restart.json"):
                self.authority.require("deploy", manifest.id, manifest.binding)
                intent = read_json(root / "restart.json")
                if intent.get("release_id") != manifest.id:
                    raise Conflict("release_pending_manifest_required")
                runtime = self.runtime_factory(manifest, path)
                record = runtime.record()
                if record and record.get("state") == "starting":
                    runtime.clear_absent_start()
                elif record and record.get("pid"):
                    runtime.health()
                with existing_database(manifest.database) as db:
                    db.execute("UPDATE release_control SET draining=NULL WHERE id=1 AND draining=?",
                               (manifest.id,))
                (root / "restart.json").unlink()
                return {"id": manifest.id, "state": "restart_reconciled"}
            if not pending or pending.get("candidate") != str(path):
                raise Conflict("release_pending_manifest_required")
            self.authority.require("deploy", manifest.id, manifest.binding)
            if pending["stage"] == "completed":
                self._complete(manifest, pending["result"])
                return {"id": manifest.id, "state": pending["result"]}
            selection = read_json(root / "selection.json", {})
            previous_path = pending["previous"]
            previous = Manifest.load(previous_path) if previous_path else None
            journal = Journal(root)
            if (pending["stage"] in {"draining", "switching"}
                    and selection.get("current") == previous_path):
                if selection.get("current") != previous_path:
                    raise Conflict("release_pending_selection_changed")
                if previous:
                    self._restore_previous(previous, previous_path)
                elif not port_free(manifest.port):
                    raise Conflict("release_unmanaged_port")
                state = "prepared_cancelled"
            elif (pending["stage"] == "rolling_back"
                  and selection.get("current") == previous_path and previous):
                self._restore_previous(previous, previous_path)
                journal.event(manifest.id, "rollback", "release_code_rollback",
                              event_key=manifest.id + ":rollback", commit=previous.commit)
                state = "rolled_back"
            elif selection.get("current") == str(path):
                runtime = self.runtime_factory(manifest, path)
                try:
                    runtime.health()
                    with existing_database(manifest.database) as db:
                        if (not pending.get("data")
                                or data_binding(db, pending["data"]) != pending["data"]
                                or not compatible(pending["schema"], schema(db))):
                            raise Conflict("release_original_data_changed")
                    journal.event(manifest.id, "deployed", event_key=manifest.id + ":deployed",
                                  commit=manifest.commit,
                                  maintenance_task=manifest.maintenance_task)
                    Supervisor(root).status(manifest, "healthy")
                    state = "healthy"
                except Exception:
                    if previous:
                        self._rollback(manifest, path, previous, previous_path, journal)
                        state = "rolled_back"
                    else:
                        runtime.stop()
                        atomic_json(root / "selection.json", {})
                        Supervisor(root).status(manifest, "blocked", "release_startup_failed")
                        state = "stopped"
            else:
                raise Conflict("release_pending_selection_changed")
            self._complete(manifest, state)
            return {"id": manifest.id, "state": state}


class Releases:
    def __init__(self, store, maintenance, analyses, repairs, acceptance, manifest=None):
        self.store, self.maintenance = store, maintenance
        self.analyses, self.repairs, self.acceptance = analyses, repairs, acceptance
        self.manifest = manifest
        self.closing = asyncio.Event()

    def authorize_resume(self, db, source_id):
        manifest = self.manifest
        authorization = db.execute(
            "SELECT binding,resume_original FROM release_authorizations "
            "WHERE kind='deploy' AND target=? AND revoked=0", (manifest.id,)).fetchone()
        task = db.execute("SELECT * FROM maintenance_tasks WHERE id=?",
                          (manifest.maintenance_task,)).fetchone()
        occurrence = db.execute("SELECT * FROM system_fault_occurrences WHERE id=?",
                                (task["occurrence_id"],)).fetchone() if task else None
        config = db.execute("SELECT enabled FROM maintenance_configuration WHERE id=1").fetchone()
        from .coordination import source_budget

        if (not authorization or not authorization["resume_original"]
                or json.loads(authorization["binding"]) != manifest.binding
                or not config or not config[0] or not occurrence
                or occurrence["task_id"] != source_id
                or occurrence["state"] not in {"pending", "blocked"}
                or task["basis"] != self.maintenance.basis(db, occurrence)
                or self.store.draining(db)):
            raise Conflict("release_source_basis_changed")
        source_budget(db, occurrence, capacity=True, maintenance=True)
        root = db.execute("SELECT * FROM recovery_roots WHERE id=?", (task["root_id"],)).fetchone()
        if (not root or root["action_id"] or root["stop_reason"]
                or root["requests"] >= recovery.MAX_REQUESTS
                or root["tokens"] >= recovery.MAX_TOKENS
                or root["seconds"] >= recovery.MAX_SECONDS
                or root["resumptions"] >= recovery.MAX_RESUMPTIONS):
            raise Conflict("release_original_budget_exhausted")
        if not db.execute(
                "UPDATE release_resumptions SET state='verifying_original' WHERE release_id=? "
                "AND state='dispatching'", (manifest.id,)).rowcount:
            raise Conflict("release_resume_receipt_changed")
        db.execute(
            "INSERT INTO release_events(id,release_id,kind,reason,created,details) "
            "VALUES(?,?,'source_resume_authorized',NULL,?,?)",
            ("resume:" + manifest.id, manifest.id, time.time(), json.dumps({
                "occurrence_id": occurrence["id"], "flow": occurrence["flow"],
                "task_id": source_id, "workbench_version": self.store.workbench_version,
            })))

    def state(self):
        with self.store.connection() as db:
            events = [dict(row) for row in db.execute(
                "SELECT id,release_id,kind,reason,created,details FROM release_events "
                "ORDER BY created DESC LIMIT 20")]
            resume = [dict(row) for row in db.execute(
                "SELECT * FROM release_resumptions ORDER BY created DESC LIMIT 20")]
            draining = db.execute("SELECT draining FROM release_control WHERE id=1").fetchone()[0]
        return {
            "managed": self.manifest is not None, "draining": bool(draining),
            "merge": "exact_head_authorization", "deploy": "exact_manifest_authorization",
            "current": {"id": self.manifest.id, "commit": self.manifest.commit,
                        "port": self.manifest.port} if self.manifest else None,
            "supervisor": read_json(Path(self.manifest.state_root) / "status.json", {})
            if self.manifest else None,
            "events": [{**event, "details": json.loads(event["details"])} for event in events],
            "resumptions": resume,
        }

    def resume_original(self):
        manifest = self.manifest
        if not manifest or not manifest.maintenance_task:
            return
        task_id, reason = manifest.maintenance_task, None
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM release_resumptions WHERE release_id=?",
                          (manifest.id,)).fetchone():
                return
            authorization = db.execute(
                "SELECT resume_original FROM release_authorizations "
                "WHERE kind='deploy' AND target=? AND revoked=0", (manifest.id,)).fetchone()
            if not authorization or not authorization[0]:
                return
            if not db.execute("SELECT 1 FROM release_events WHERE release_id=? AND kind='deployed'",
                              (manifest.id,)).fetchone():
                return
            task = db.execute("SELECT * FROM maintenance_tasks WHERE id=?", (task_id,)).fetchone()
            occurrence = db.execute("SELECT * FROM system_fault_occurrences WHERE id=?",
                                    (task["occurrence_id"],)).fetchone() if task else None
            try:
                local_authorization(manifest)
                from .coordination import source_budget
                from .system_faults import SystemFaults

                if (not task
                        or task["state"] not in {"pr_created", "linked_pr", "waiting_deployment"}
                        or not occurrence or occurrence["state"] not in {"blocked", "pending"}
                        or task["basis"] != self.maintenance.basis(db, occurrence)
                        or not db.execute("SELECT 1 FROM system_fault_occurrences o WHERE o.id=? "
                                          "AND " + SystemFaults._scope(db),
                                          (occurrence["id"],)).fetchone()):
                    raise Conflict("release_source_basis_changed")
                remote = json.loads(task["remote"] or "{}")
                if remote.get("number") != manifest.pr:
                    raise Conflict("release_source_pr_mismatch")
                root = db.execute("SELECT * FROM recovery_roots WHERE id=?",
                                  (task["root_id"],)).fetchone()
                if (not root or root["action_id"] or root["requests"] >= recovery.MAX_REQUESTS
                        or root["tokens"] >= recovery.MAX_TOKENS
                        or root["seconds"] >= recovery.MAX_SECONDS or root["stop_reason"]
                        or root["resumptions"] >= recovery.MAX_RESUMPTIONS):
                    raise Conflict("release_original_budget_exhausted")
                source_budget(db, occurrence, capacity=True, maintenance=True)
            except Conflict as exc:
                reason = str(exc)
            db.execute("INSERT INTO release_resumptions VALUES(?,?,?,?,?)",
                       (manifest.id, task_id, "blocked" if reason else "dispatching", reason,
                        time.time()))
        if reason:
            return
        # The original adapter validates its real checkpoint and runs its failed step.
        # No deployment receipt can mark a fault resolved or fabricate a new confirmation.
        try:
            NativeRuntime(manifest, Path(manifest.state_root) / "manifests" /
                          (manifest.id + ".json")).health()
            flow, source_id = occurrence["flow"], occurrence["task_id"]
            table, field = {"analysis": ("repository_analyses", "status"),
                            "repair": ("repair_tasks", "status"),
                            "acceptance": ("scope_acceptance_runs", "state")}[flow]
            with self.store.connection() as db:
                source = db.execute(f"SELECT * FROM {table} WHERE id=?", (source_id,)).fetchone()
                if not source or source[field] in {"cancelled", "stale", "completed", "pr_created"}:
                    raise Conflict("release_source_inactive")
            if flow == "analysis":
                self.analyses.retry(source_id, source["version"], release=self)
            elif flow == "repair":
                self.repairs.retry(source_id, source["version"], automatic=True, release=self)
            else:
                with self.store.connection() as db:
                    analysis_version = db.execute(
                        "SELECT version FROM repository_analyses WHERE id=?",
                        (source["analysis_id"],)).fetchone()[0]
                self.acceptance.retry(source["analysis_id"], analysis_version,
                                      run_id=source_id, release=self)
            state = "verifying_original"
        except Exception as exc:
            state = "blocked"
            reason = str(exc) if isinstance(exc, Conflict) else "release_checkpoint_unavailable"
        with self.store.connection() as db:
            db.execute("UPDATE release_resumptions SET state=?,reason=? WHERE release_id=?",
                       (state, reason, manifest.id))

    def reconcile_resumptions(self):
        with self.store.connection() as db:
            db.execute(
                "UPDATE release_resumptions SET state='blocked',"
                "reason='release_resume_receipt_unknown' "
                "WHERE state='dispatching' AND created<?", (time.time() - 120,))
            for row in db.execute(
                    "SELECT r.release_id,o.state FROM release_resumptions r "
                    "JOIN maintenance_tasks m ON m.id=r.task_id "
                    "JOIN system_fault_occurrences o ON o.id=m.occurrence_id "
                    "WHERE r.state='verifying_original'").fetchall():
                if row["state"] in {"resolved", "dismissed"}:
                    db.execute("UPDATE release_resumptions SET state=?,reason=? WHERE release_id=?",
                               ("completed" if row["state"] == "resolved" else "blocked",
                                "source_verified" if row["state"] == "resolved"
                                else "source_dismissed",
                                row["release_id"]))

    async def run(self):
        while not self.closing.is_set():
            try:
                if self.manifest:
                    await asyncio.to_thread(Journal(self.manifest.state_root).deliver, self.store)
                    with self.store.connection() as db:
                        drain = self.store.draining(db)
                    if not drain:
                        await asyncio.to_thread(self.resume_original)
                        await asyncio.to_thread(self.reconcile_resumptions)
            except Exception:
                pass
            await asyncio.sleep(2)
