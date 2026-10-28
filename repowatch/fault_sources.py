"""Bounded, resumable adapters over durable source facts; no execution imports."""

import json
import time

from .fault_contract import FaultEvent, diagnostic, token
from .system_faults import enqueue

EVENT_SOURCES = {
    "analysis_events": ("id", "created"),
    "repair_events": ("id", "created"),
    "scope_acceptance_failures": ("rowid", "created"),
    "task_scope_events": ("id", "created"),
}
STATE_SOURCES = {
    "execution_jobs": "id",
    "execution_diagnoses": "job_id",
    "repository_analyses": "id",
}


def load(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def initialize_cursors(db):
    for table, (key, _) in EVENT_SOURCES.items():
        high = db.execute(f"SELECT coalesce(max({key}),0) FROM {table}").fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO system_fault_ingestion(source,cursor) VALUES(?,?)",
            (table, json.dumps(high)),
        )
    for table in STATE_SOURCES:
        db.execute(
            "INSERT OR IGNORE INTO system_fault_ingestion(source,cursor) VALUES(?,?)",
            (table, json.dumps([time.time(), ""])),
        )


def environment(db, env_id):
    row = db.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    return dict(row) if row else {}


def analysis_context(db, analysis_id):
    row = db.execute("SELECT * FROM repository_analyses WHERE id=?", (analysis_id,)).fetchone()
    return dict(row) if row else {}


def previous(db, table, task_column, task_id, event_id, kind, column):
    start = db.execute(
        f"SELECT coalesce(max(id),0) FROM {table} WHERE {task_column}=? AND id<=? "
        "AND kind='fault_attempt'",
        (task_id, event_id),
    ).fetchone()[0]
    row = db.execute(
        f"SELECT {column} FROM {table} WHERE {task_column}=? AND id<=? AND id>=? AND kind=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, event_id, start, kind),
    ).fetchone()
    return row[0] if row else None


def binding(db, table, task_column, task_id, event_id, role, column):
    start = db.execute(
        f"SELECT coalesce(max(id),0) FROM {table} WHERE {task_column}=? AND id<=? "
        "AND kind='fault_attempt'",
        (task_id, event_id),
    ).fetchone()[0]
    rows = db.execute(
        f"SELECT {column} FROM {table} WHERE {task_column}=? AND id<=? AND id>=? "
        "AND kind='model_configuration' ORDER BY id DESC LIMIT 12",
        (task_id, event_id, start),
    ).fetchall()
    for row in rows:
        value = load(row[0])
        if value.get("role") == role:
            return diagnostic(value)
    return {}


def category(code):
    if "timeout" in code:
        return "model_timeout"
    if any(
        value in code
        for value in (
            "read_not_followed",
            "handoff",
            "tool_arguments",
            "json_invalid",
            "output_invalid",
            "batch",
        )
    ):
        return "protocol"
    if any(value in code for value in ("unreachable", "unavailable", "rate_limit", "provider")):
        return "external_service"
    if "review_rejected" in code:
        return "review_rejection"
    if code in ("tests_failed", "test_failed", "repair_tests_failed", "acceptance_check_failed"):
        return "target_test"
    if any(value in code for value in ("docker", "setup", "install", "runtime", "start_")):
        return "environment"
    return "unknown"


def observation(db, event, state, reason, *, code=None):
    if state == "resolved" and any(
        getattr(event, key) is None
        for key in (
            "attempt",
            "revision",
            "workbench_version",
            "repo_id",
            "account_id",
            "env_id",
        )
    ):
        # Legacy source success cannot establish which unknown basis was verified.
        return
    payload = {
        key: event.safe()[key]
        for key in (
            "source_event_key",
            "task_id",
            "occurred",
            "attempt",
            "flow",
            "role",
            "diagnostic",
            "budget",
            "repo_id",
            "account_id",
            "env_id",
            "revision",
            "workbench_version",
        )
    }
    payload.update(state=state, reason=token(reason) or "source_observation", code=token(code))
    if state == "resolved":
        if event.flow in ("analysis", "repair"):
            table, task_column, column, roles = (
                (
                    "analysis_events",
                    "analysis_id",
                    "agent",
                    (
                        "repository_analyst",
                        "problem_investigator",
                        "reproduction_assistant",
                        "independent_reviewer",
                    ),
                )
                if event.flow == "analysis"
                else (
                    "repair_events",
                    "repair_id",
                    "value",
                    ("repair_agent", "independent_reviewer"),
                )
            )
            payload["verified_models"] = [
                model
                for role in roles
                if (
                    model := binding(
                        db,
                        table,
                        task_column,
                        event.task_id,
                        int(event.source_id),
                        role,
                        column,
                    )
                )
            ]
        elif event.flow == "acceptance":
            model = acceptance_binding(db, event.task_id, event.attempt, event.occurred)
            if model:
                payload["verified_models"] = [model]
    db.execute(
        "INSERT OR IGNORE INTO system_fault_outbox(source_event_key,kind,payload,created) "
        "VALUES(?,'observation',?,?)",
        (event.source_event_key, json.dumps(payload), time.time()),
    )


def base_event(
    db,
    table,
    source,
    version,
    historical,
    *,
    flow,
    task_id,
    analysis=None,
    env_id=None,
    attempt=None,
    role=None,
    stage=None,
):
    analysis = analysis or {}
    env_id = env_id or analysis.get("env_id")
    env = environment(db, env_id)
    return FaultEvent(
        source_event_key=f"{table}:{source['_key']}",
        flow=flow,
        task_id=task_id,
        root_task_id=analysis.get("id") or task_id,
        source_table=table,
        source_id=str(source["_key"]),
        occurred=float(source.get("created", source.get("updated"))),
        repo_id=analysis.get("repo_id") or env.get("repo_id"),
        account_id=env.get("account_id"),
        env_id=env_id,
        attempt=attempt,
        role=role,
        stage=stage,
        revision=analysis.get("revision") or env.get("revision"),
        workbench_version=version if source.get("_live") else None,
        historical=historical,
    )


def analysis_event(db, source, version, historical):
    task_id, kind, event_id = source["analysis_id"], source["kind"], source["id"]
    data = load(source["agent"])
    analysis = analysis_context(db, task_id)
    recovery = db.execute(
        "SELECT * FROM analysis_report_recovery WHERE analysis_id=?", (task_id,)
    ).fetchone()
    # New events include a stable attempt binding; legacy attempts remain unknown.
    meta = previous(
        db, "analysis_events", "analysis_id", task_id, event_id, "fault_attempt", "agent"
    )
    attempt_data = load(meta)
    role = data.get("role") or data.get("stage")
    if not role:
        role = previous(db, "analysis_events", "analysis_id", task_id, event_id, "agent", "agent")
    event = base_event(
        db,
        "analysis_events",
        source,
        version,
        historical,
        flow="analysis",
        task_id=task_id,
        analysis=analysis,
        attempt=attempt_data.get("attempt"),
        role=role,
        stage=data.get("stage") or role,
    )
    event.workbench_version = attempt_data.get("workbench_version")
    event.model_binding = binding(
        db, "analysis_events", "analysis_id", task_id, event_id, role, "agent"
    )
    event.trace_id = previous(
        db, "analysis_events", "analysis_id", task_id, event_id, "langfuse_trace", "agent"
    )
    event.budget = load(
        previous(
            db,
            "analysis_events",
            "analysis_id",
            task_id,
            event_id,
            "analysis_retry_budget",
            "agent",
        )
    )
    if source.get("_live") and recovery:
        event.budget.update({key: recovery[key] for key in ("requests", "resumptions", "deadline")})
        event.budget.update(max_requests=32, max_resumptions=3, stop_reason=recovery["stop_reason"])
    event.budget.update(diagnostic(data.get("budget", {})))
    event.diagnostic = data
    if kind in ("required_read_validation", "handoff_validation") and data.get("accepted") is False:
        required = data.get("required")
        event.code = (
            f"analysis_{required}_read_not_followed"
            if required
            else "analysis_handoff_not_followed"
        )
        event.execution = "not_executed"
        event.category = "protocol"
        enqueue(db, event)
    elif kind == "model_request_failed":
        event.code = "analysis_provider_timeout"
        event.category = "model_timeout"
        # A timed out request may have executed at the provider; no tool was run locally.
        event.execution = "unknown"
        enqueue(db, event)
    elif kind in ("tool_arguments_invalid", "output_limit", "report_diagnostics"):
        event.code = {
            "tool_arguments_invalid": "analysis_tool_arguments_invalid",
            "output_limit": "analysis_output_truncated",
            "report_diagnostics": "analysis_report_invalid",
        }[kind]
        event.category = category(event.code)
        event.execution = "not_executed"
        enqueue(db, event)
    elif kind == "failure":
        event.code = data.get("code") or "unknown"
        event.category = category(event.code)
        event.diagnostic = {
            **data,
            "attempt_elapsed_seconds": data.get("elapsed_seconds"),
            "timing_scope": "attempt",
            "timeout_kind": "unknown",
        }
        # A failed prerequisite and its terminal wrapper are one occurrence.
        prior_kind = (
            "handoff_validation"
            if "handoff" in event.code
            else "required_read_validation"
            if "read_not_followed" in event.code
            else None
        )
        prior = (
            load(
                previous(
                    db, "analysis_events", "analysis_id", task_id, event_id, prior_kind, "agent"
                )
            )
            if prior_kind
            else {}
        )
        if prior.get("accepted") is False:
            observation(db, event, "blocked", event.code, code=event.code)
        elif "timeout" in event.code and previous(
            db,
            "analysis_events",
            "analysis_id",
            task_id,
            event_id,
            "model_request_failed",
            "agent",
        ):
            event.diagnostic["stop_reason"] = event.code
            observation(db, event, "blocked", event.code, code="analysis_provider_timeout")
        else:
            enqueue(db, event)
            if event.category != "unknown":
                event.source_event_key += ":stopped"
                observation(db, event, "blocked", event.code, code=event.code)
    elif kind in (
        "timeout_retry",
        "required_read_retry",
        "tool_arguments_retry",
        "report_recovery_scheduled",
        "report_recovery_resumed",
    ):
        observation(db, event, "recovering", kind)
    elif kind == "fault_verified":
        observation(db, event, "resolved", "analysis_report_validated")
    elif kind in ("report_recovery_stopped",):
        observation(db, event, "blocked", source["agent"])


def repair_event(db, source, version, historical):
    row = db.execute("SELECT * FROM repair_tasks WHERE id=?", (source["repair_id"],)).fetchone()
    if not row:
        return
    task = dict(row)
    kind, data = source["kind"], load(source["value"])
    role = data.get("role") or previous(
        db, "repair_events", "repair_id", task["id"], source["id"], "agent", "value"
    )
    attempt_data = load(
        previous(
            db, "repair_events", "repair_id", task["id"], source["id"], "fault_attempt", "value"
        )
    )
    attempt = attempt_data.get("attempt")
    published = db.execute(
        "SELECT max(id) FROM repair_events WHERE repair_id=? AND id<=? AND kind='publishing'",
        (task["id"], source["id"]),
    ).fetchone()[0]
    attempt_start = db.execute(
        "SELECT coalesce(max(id),0) FROM repair_events WHERE repair_id=? AND id<=? "
        "AND kind='fault_attempt'",
        (task["id"], source["id"]),
    ).fetchone()[0]
    publishing = bool(published and published >= attempt_start)
    if publishing:
        role = None
    event = base_event(
        db,
        "repair_events",
        source,
        version,
        historical,
        flow="publication" if publishing else "repair",
        task_id=task["id"],
        analysis=analysis_context(db, task["analysis_id"]),
        env_id=task["env_id"],
        attempt=attempt,
        role=role,
        stage="publishing" if publishing else role,
    )
    event.workbench_version = attempt_data.get("workbench_version")
    event.model_binding = binding(
        db, "repair_events", "repair_id", task["id"], source["id"], role, "value"
    )
    event.trace_id = previous(
        db, "repair_events", "repair_id", task["id"], source["id"], "langfuse_trace", "value"
    )
    event.budget = {"attempt": attempt, "attempt_limit": attempt_data.get("attempt_limit")}
    event.budget.update(diagnostic(data.get("budget", {})))
    event.diagnostic = data
    if kind == "failure_details":
        event.code = data.get("code") or "unknown"
        event.category = category(event.code)
        event.execution = (
            "not_executed"
            if event.category == "protocol"
            else "executed"
            if event.category in ("target_test", "review_rejection")
            else "unknown"
        )
        if event.code == "repair_model_timeout" and previous(
            db, "repair_events", "repair_id", task["id"], source["id"], "request_timeout", "value"
        ):
            observation(db, event, "blocked", event.code, code=event.code)
        else:
            enqueue(db, event)
    elif kind == "failure":
        previous_details = load(
            previous(
                db,
                "repair_events",
                "repair_id",
                task["id"],
                source["id"],
                "failure_details",
                "value",
            )
        )
        event.code = source["value"]
        event.category = category(event.code)
        if previous_details.get("code") != event.code:
            enqueue(db, event)
        elif event.category != "unknown":
            observation(db, event, "blocked", event.code, code=event.code)
    elif kind in ("tool_arguments_invalid", "handoff_validation"):
        if kind == "handoff_validation" and data.get("accepted") is not False:
            return
        event.code = "repair_" + kind
        event.category, event.execution = "protocol", "not_executed"
        enqueue(db, event)
    elif kind == "request_timeout":
        event.code, event.category = "repair_model_timeout", "model_timeout"
        event.diagnostic = {
            **data,
            "request_elapsed_seconds": data.get("elapsed_seconds"),
            "request_timeout_seconds": data.get("timeout_seconds"),
            "timing_scope": "request",
            "timeout_kind": "unknown",
        }
        enqueue(db, event)
    elif kind in ("verification_feedback", "review_feedback"):
        event.code = (
            "repair_review_rejected" if kind == "review_feedback" else "repair_tests_failed"
        )
        event.category, event.execution = category(event.code), "executed"
        enqueue(db, event)
    elif kind.endswith("recovery_queued") or kind in ("verification_resume", "timeout_retry"):
        observation(db, event, "recovering", kind)
    elif kind == "review_approved":
        observation(db, event, "resolved", "patch_tests_and_independent_review_passed")
    elif kind == "pr_created":
        event.flow = "publication"
        observation(db, event, "resolved", "publication_receipt_verified")


def acceptance_failure(db, source, version, historical):
    row = db.execute(
        "SELECT * FROM scope_acceptance_runs WHERE id=?", (source["run_id"],)
    ).fetchone()
    if not row:
        return
    task = dict(row)
    event = base_event(
        db,
        "scope_acceptance_failures",
        source,
        version,
        historical,
        flow="acceptance",
        task_id=task["id"],
        analysis=analysis_context(db, task["analysis_id"]),
        attempt=task["attempt"],
        role="independent_reviewer",
        stage=task["stage"],
    )
    event.code = source["code"]
    event.category = category(event.code)
    event.diagnostic = load(source["details"])
    basis = event.diagnostic.get("fault_basis", {})
    event.workbench_version = basis.get("workbench_version")
    event.attempt = basis.get("attempt")
    event.budget = diagnostic(basis.get("budget", {}))
    event.budget["retryable"] = bool(source["retryable"])
    event.model_binding = acceptance_binding(db, task["id"], event.attempt, source["created"])
    enqueue(db, event)


def acceptance_binding(db, run_id, attempt, occurred):
    if attempt is None:
        return {}
    models = db.execute(
        "SELECT value FROM task_scope_events WHERE scope_id=? "
        "AND kind='acceptance_model_configuration'"
        " AND created<=? ORDER BY id DESC LIMIT 8",
        (
            db.execute(
                "SELECT scope_id FROM scope_acceptance_runs WHERE id=?", (run_id,)
            ).fetchone()[0],
            occurred,
        ),
    )
    for model in models:
        value = load(model[0])
        if (
            value.get("run_id") == run_id
            and value.get("attempt") == attempt
            and value.get("role") == "independent_reviewer"
        ):
            # attempt/run identity is separate from the actual model configuration.
            return diagnostic(
                {key: item for key, item in value.items() if key not in ("attempt", "run_id")}
            )
    return {}


def scope_event(db, source, version, historical):
    if source["kind"] not in (
        "acceptance_completed",
        "acceptance_retry_requested",
        "acceptance_check_finished",
        "acceptance_check_blocked",
        "acceptance_repair_queued",
    ):
        return
    data = load(source["value"])
    task = db.execute(
        "SELECT * FROM scope_acceptance_runs WHERE id=?", (data.get("run_id"),)
    ).fetchone()
    if not task:
        return
    event = base_event(
        db,
        "task_scope_events",
        source,
        version,
        historical,
        flow="acceptance",
        task_id=task["id"],
        analysis=analysis_context(db, task["analysis_id"]),
        attempt=data.get("attempt", task["attempt"]),
    )
    event.workbench_version = data.get("workbench_version")
    if source["kind"] in ("acceptance_check_finished", "acceptance_check_blocked"):
        if data.get("passed") is True:
            return
        event.code = (
            "acceptance_check_failed"
            if data.get("executed") is True
            else "acceptance_check_blocked"
        )
        event.category = "target_test" if data.get("executed") is True else "environment"
        event.execution = "executed" if data.get("executed") is True else "not_executed"
        event.stage = "verification"
        event.role = "independent_reviewer"
        event.diagnostic = data
        enqueue(db, event)
        return
    observation(
        db,
        event,
        "resolved" if source["kind"] == "acceptance_completed" else "recovering",
        source["kind"],
    )


def state_event(db, table, source, version, historical):
    if table == "repository_analyses":
        if source["status"] not in ("failed", "interrupted") or not source["error"]:
            return
        # Terminal failures with source events were already captured with richer diagnostics.
        latest = db.execute(
            "SELECT created FROM analysis_events WHERE analysis_id=? AND kind='failure' "
            "ORDER BY id DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        if latest and latest[0] >= source["updated"] - 5:
            return
        event = base_event(
            db,
            table,
            source,
            version,
            historical,
            flow="analysis",
            task_id=source["id"],
            analysis=source,
        )
        event.code = source["error"]
    elif table == "execution_jobs":
        if source["state"] != "failed":
            return
        event = base_event(
            db,
            table,
            source,
            version,
            historical,
            flow="execution",
            task_id=source["id"],
            env_id=source["env_id"],
            attempt=1,
            stage=source["action"],
        )
        event.code = source["error"] or "unknown"
        basis = load(source.get("fault_basis"))
        event.revision = basis.get("revision")
        event.workbench_version = basis.get("workbench_version")
        event.execution = basis.get("execution", "unknown")
        if event.code in ("test_failed", "tests_failed", "setup_failed", "start_not_listening"):
            event.execution = "executed"
    else:
        if source["state"] != "failed":
            return
        event = base_event(
            db,
            table,
            source,
            version,
            historical,
            flow="diagnosis",
            task_id=source["job_id"],
            env_id=source["env_id"],
        )
        report = load(source["report"])
        event.code = "diagnosis_" + report.get("model_status", "unknown")
        event.diagnostic = load(source.get("fault_diagnostic"))
        event.role = source.get("fault_role")
        event.model_binding = load(source.get("fault_model_binding")).get(event.role, {})
        event.workbench_version = source.get("fault_workbench_version")
        event.revision = source["revision"]
    event.category = category(event.code)
    enqueue(db, event)


ADAPTERS = {
    "analysis_events": analysis_event,
    "repair_events": repair_event,
    "scope_acceptance_failures": acceptance_failure,
    "task_scope_events": scope_event,
}


def capture(db, version, *, historical=False, limit=100, live_after=None):
    if not db.execute("SELECT enabled FROM system_fault_configuration").fetchone()[0]:
        return 0
    scanned = 0
    installed = db.execute("SELECT installed FROM system_fault_configuration").fetchone()[0]
    for table, (key, created) in EVENT_SOURCES.items():
        source_name = ("history:" if historical else "") + table
        saved = db.execute(
            "SELECT cursor FROM system_fault_ingestion WHERE source=?", (source_name,)
        ).fetchone()
        cursor = json.loads(saved[0]) if saved else 0
        query = f"SELECT *,{key} AS _key FROM {table} WHERE {key}>?"
        args = [cursor]
        if historical:
            query += f" AND {created}<?"
            args.append(installed)
        rows = db.execute(query + f" ORDER BY {key} LIMIT ?", [*args, limit]).fetchall()
        for row in rows:
            value = dict(row)
            value["_live"] = live_after is not None and row[created] >= live_after
            ADAPTERS[table](db, value, version, historical)
            cursor = row["_key"]
        save_cursor(db, source_name, cursor)
        scanned += len(rows)
    for table, key in STATE_SOURCES.items():
        source_name = ("history:" if historical else "") + table
        saved = db.execute(
            "SELECT cursor FROM system_fault_ingestion WHERE source=?", (source_name,)
        ).fetchone()
        cursor = json.loads(saved[0]) if saved else [0, ""]
        query = f"SELECT * FROM {table} WHERE (updated>? OR (updated=? AND {key}>?))"
        args = [cursor[0], cursor[0], cursor[1]]
        if historical:
            query += " AND updated<?"
            args.append(installed)
        rows = db.execute(query + f" ORDER BY updated,{key} LIMIT ?", [*args, limit]).fetchall()
        for row in rows:
            value = dict(row)
            identity = value.get("version", value["updated"])
            value["_key"] = f"{value[key]}:{identity}"
            # State-only sources use the durable update time, never the ingestion time.
            value["created"] = value["updated"]
            value["_live"] = live_after is not None and value["updated"] >= live_after
            state_event(db, table, value, version, historical)
            cursor = [value["updated"], value[key]]
        save_cursor(db, source_name, cursor)
        scanned += len(rows)
    return scanned


def save_cursor(db, source, cursor):
    db.execute(
        "INSERT INTO system_fault_ingestion(source,cursor,last_success) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,"
        "last_success=excluded.last_success,error=NULL",
        (source, json.dumps(cursor), time.time()),
    )
