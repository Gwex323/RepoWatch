"""Versioned flash-monitoring / v4pro-repair escalation policy.

The policy is an additive, default-off routing layer. It never stores or
returns provider credentials, never changes ``agent_model_settings.routes``,
and never grants the model any tool or publication capability.
"""

import json
import time

from .model_providers import ModelProviders
from .roles import COORDINATOR, REPAIR_ROLE
from .store import Conflict

MONITOR_ROLES = (
    COORDINATOR,
    "repository_analyst",
    "problem_investigator",
    "reproduction_assistant",
    "independent_reviewer",
)


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS model_escalation_policy (
            id INTEGER PRIMARY KEY CHECK(id=1),
            enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            monitor_route TEXT NOT NULL DEFAULT 'null',
            repair_route TEXT NOT NULL DEFAULT 'null',
            updated REAL NOT NULL
        )
    """)
    db.execute(
        "INSERT OR IGNORE INTO model_escalation_policy(id,updated) VALUES(1,?)",
        (time.time(),),
    )


class ModelEscalation:
    """Read and apply the owner-selected escalation policy."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def policy_row(db):
        row = db.execute("SELECT * FROM model_escalation_policy WHERE id=1").fetchone()
        return dict(row) if row else None

    @staticmethod
    def route(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return None
        if not isinstance(value, dict):
            return None
        provider_id = value.get("provider_id")
        model = value.get("model")
        if (not isinstance(provider_id, str) or not provider_id
                or not isinstance(model, str) or not model):
            return None
        return {"provider_id": provider_id, "model": model}

    @classmethod
    def resolve_route(cls, db, route):
        route = cls.route(route)
        if route is None:
            raise Conflict("model_escalation_route_required")
        resolved = ModelProviders.resolve(db, route)
        if not resolved["encrypted_key"]:
            raise Conflict("analysis_model_key_required")
        return {
            "route": route,
            "provider_id": resolved["provider_id"],
            "provider_name": resolved["provider_name"],
            "provider_version": resolved["provider_version"],
            "model": resolved["config"]["model"],
            "config": resolved["config"],
            "encrypted_key": resolved["encrypted_key"],
            "has_key": bool(resolved["encrypted_key"]),
        }

    @classmethod
    def _resolved_policy(cls, db, policy=None):
        policy = policy if policy is not None else cls.policy_row(db)
        if not policy or not policy["enabled"]:
            return policy, None, None
        return (
            policy,
            cls.resolve_route(db, policy["monitor_route"]),
            cls.resolve_route(db, policy["repair_route"]),
        )

    @classmethod
    def _apply_routes(cls, rows, policy, monitor, repair, *, flow, repair_allowed):
        applied = {}
        for role, value in rows.items():
            if role == REPAIR_ROLE:
                choice = repair if repair_allowed else monitor
                route_kind = "repair" if repair_allowed else "monitor"
            elif role in MONITOR_ROLES:
                choice, route_kind = monitor, "monitor"
            else:
                applied[role] = value
                continue
            applied[role] = {**value, "config": json.dumps(choice["config"]),
                             "encrypted_key": choice["encrypted_key"],
                             "provider_id": choice["provider_id"],
                             "provider_name": choice["provider_name"],
                             "provider_version": choice["provider_version"],
                             "selection_choice": choice["route"],
                             "routing_version": policy["version"],
                             "escalation_route": route_kind,
                             "escalation_version": policy["version"]}
        info = {
            "enabled": True,
            "version": policy["version"],
            "monitor": {
                "model": monitor["config"]["model"],
                "provider_id": monitor["provider_id"],
                "provider_name": monitor["provider_name"],
                "provider_version": monitor["provider_version"],
            },
            "repair": {
                "model": repair["config"]["model"],
                "provider_id": repair["provider_id"],
                "provider_name": repair["provider_name"],
                "provider_version": repair["provider_version"],
            },
            "flow": flow,
            "repair_model_used": repair_allowed,
        }
        return applied, info

    @classmethod
    def apply_analysis(cls, db, rows, policy=None):
        """Canonical, side-effect-free analysis routing for identity and execution."""
        policy, monitor, repair = cls._resolved_policy(db, policy)
        if not policy or not policy["enabled"]:
            return rows, None
        return cls._apply_routes(
            rows, policy, monitor, repair, flow="analysis", repair_allowed=False,
        )

    @classmethod
    def state_from_row(cls, db, row=None):
        row = row or cls.policy_row(db)
        if row is None:
            return {
                "enabled": False,
                "version": 0,
                "monitor_route": None,
                "repair_route": None,
                "monitor": None,
                "repair": None,
                "ready": False,
                "missing": ["model_escalation_route_required"] * 2,
            }
        routes = {
            "monitor": cls.route(row["monitor_route"]),
            "repair": cls.route(row["repair_route"]),
        }
        status = {"monitor": None, "repair": None}
        missing = []
        for key in ("monitor", "repair"):
            route = routes[key]
            if route is None:
                missing.append("model_escalation_route_required")
                continue
            try:
                status[key] = cls.resolve_route(db, route)
                status[key].pop("config", None)
                status[key].pop("encrypted_key", None)
            except Conflict as exc:
                missing.append(str(exc))
                status[key] = None
        return {
            "enabled": bool(row["enabled"]),
            "version": row["version"],
            "monitor_route": routes["monitor"],
            "repair_route": routes["repair"],
            "monitor": status["monitor"],
            "repair": status["repair"],
            "ready": bool(row["enabled"] and not missing),
            "missing": missing,
        }

    def state(self):
        with self.store.connection() as db:
            return self.state_from_row(db)

    def configure(self, *, version, enabled, monitor_route, repair_route):
        monitor_route = self.route(monitor_route)
        repair_route = self.route(repair_route)
        if enabled and (monitor_route is None or repair_route is None):
            raise Conflict("model_escalation_route_required")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.policy_row(db)
            if version != (row["version"] if row else 0):
                raise Conflict("model_escalation_changed")
            if enabled:
                # Fail closed: both routes must resolve and have saved credentials.
                self.resolve_route(db, monitor_route)
                self.resolve_route(db, repair_route)
            else:
                monitor_route = monitor_route or {"provider_id": "", "model": ""}
                repair_route = repair_route or {"provider_id": "", "model": ""}
            db.execute(
                "UPDATE model_escalation_policy SET enabled=?,version=version+1,"
                "monitor_route=?,repair_route=?,updated=? WHERE id=1",
                (
                    int(enabled),
                    json.dumps(monitor_route, ensure_ascii=False),
                    json.dumps(repair_route, ensure_ascii=False),
                    time.time(),
                ),
            )
        return self.state()

    @classmethod
    def repair_authorized(cls, db, context):
        """Return true only when a durable, currently authorized repair task exists."""
        if context.get("_maintenance_classification"):
            return False
        # The flag is a server-side guard, not an independent authorization proof.
        if context.get("_model_escalation_authorized") is not True:
            return False
        task_id = context.get("_repair_task_id")
        if not isinstance(task_id, str) or not task_id:
            return False
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repair_tasks'"
        ).fetchone():
            return False
        row = db.execute(
            "SELECT * FROM repair_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row or row["status"] != "running":
            return False
        if context.get("revision") and row["revision"] != context["revision"]:
            return False
        return True

    @classmethod
    def apply(cls, db, rows, *, flow, context, force_monitor=False, policy=None):
        row, monitor, repair = cls._resolved_policy(db, policy)
        if not row or not row["enabled"]:
            return rows, None
        repair_allowed = (
            flow == "repair"
            and not force_monitor
            and cls.repair_authorized(db, context)
        )
        if flow == "repair" and not force_monitor and not repair_allowed:
            # A repair phase must never silently downgrade code editing to the
            # monitoring model when its durable authorization is no longer valid.
            raise Conflict("model_escalation_authorization_required")
        return cls._apply_routes(
            rows, row, monitor, repair, flow=flow, repair_allowed=repair_allowed,
        )

    @staticmethod
    def snapshot(rows, escalation):
        """Safe phase snapshot: identities only, never configs, keys or ciphertext."""
        return {
            "policy_version": escalation["version"],
            "monitor": escalation["monitor"],
            "repair": escalation["repair"],
            "repair_model_used": escalation["repair_model_used"],
            "role_models": {
                role: {
                    "model": json.loads(value["config"]).get("model", ""),
                    "model_version": value.get("version", 0),
                    "provider_id": value.get("provider_id"),
                    "provider_name": value.get("provider_name"),
                    "provider_version": value.get("provider_version"),
                    "escalation_route": value.get("escalation_route"),
                }
                for role, value in rows.items()
            },
        }

    @classmethod
    def record_repair_snapshot(cls, db, repair_id, rows, escalation, *, context=None):
        """Persist the phase snapshot in the repair task without overwriting other context."""
        context = dict(context or {})
        snapshot = cls.snapshot(rows, escalation)
        persisted = cls.persistable_context(context)
        persisted["model_escalation_snapshot"] = snapshot
        db.execute(
            "UPDATE repair_tasks SET context=? WHERE id=?",
            (json.dumps(persisted, ensure_ascii=False), repair_id),
        )
        context["model_escalation_snapshot"] = snapshot
        return context

    @staticmethod
    def persistable_context(context):
        """Remove the in-memory authorization guard before persisting task context."""
        return {
            key: value for key, value in context.items()
            if key != "_model_escalation_authorized"
        }
