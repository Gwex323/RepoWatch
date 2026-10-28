"""Saved provider catalog and owner-selected models; no network requests here."""

import json

from .model_urls import valid_model_base_url
from .roles import BUSINESS_ROLES, COORDINATOR, FLOW_ROLES, ROLE_DETAILS, ROLES
from .store import Conflict, Missing, uid

REASONING_EFFORTS = {"auto", "default", "low", "medium", "high", "max"}
AGENT_ROLES = {role: ROLE_DETAILS[role]["name"] for role in ROLES}


def validate_model_config(model, base_url, api, reasoning_effort="auto"):
    model, base_url = model.strip(), base_url.strip().rstrip("/")
    valid = valid_model_base_url(base_url)
    if (not valid or not model or len(model) > 200 or len(base_url) > 500
            or api not in ("responses", "chat_completions")
            or reasoning_effort not in REASONING_EFFORTS
            or model.lower() == "glm-5.3" and reasoning_effort == "medium"):
        raise Conflict("analysis_model_invalid")
    return {"model": model, "base_url": base_url, "api": api,
            "reasoning_effort": reasoning_effort}


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
            api TEXT NOT NULL, models TEXT NOT NULL, encrypted_key TEXT NOT NULL,
            version INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_model_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
            enabled INTEGER, routes TEXT NOT NULL
        )
    """)
    db.execute("INSERT OR IGNORE INTO agent_model_settings VALUES(1,0,NULL,'{}')")
    marker = "model_provider_five_roles_v1"
    if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
        return
    settings = dict(db.execute("SELECT * FROM agent_model_settings WHERE id=1").fetchone())
    old = json.loads(settings["routes"])
    routes = {"default": old["default"]} if old.get("default") else {}
    notes = []
    legacy = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
    legacy_config = json.loads(legacy["config"]) if legacy else {}
    legacy_config.pop("enabled", None)
    flow_names = {"analysis": "仓库分析", "repair": "自动修复", "diagnosis": "环境诊断"}
    for role in BUSINESS_ROLES:
        choices = [("角色", old[role])] if old.get(role) else []
        choices.extend((flow_names[flow], old[f"{flow}.{role}"])
                       for flow in ("analysis", "repair", "diagnosis")
                       if role in FLOW_ROLES[flow] and old.get(f"{flow}.{role}"))
        if choices:
            routes[role] = choices[0][1]
            if len({json.dumps(choice, sort_keys=True) for _, choice in choices}) > 1:
                notes.append(f"{AGENT_ROLES[role]}：旧流程选择不同，已采用{choices[0][0]}中的选择，"
                             "可在角色卡片中修改。")
        direct = db.execute("SELECT * FROM agent_models WHERE role=?", (role,)).fetchone()
        if (direct and direct["encrypted_key"] and (old.get("default") or choices)
                and (json.loads(direct["config"]) != legacy_config
                     or not legacy or direct["encrypted_key"] != legacy["encrypted_key"])):
            routes[role] = {"direct": True}
            notes.append(f"{AGENT_ROLES[role]}：保留此前单独保存的角色配置。")
    db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",
               ("model_provider_routes_before_five_roles", json.dumps(settings)))
    db.execute("UPDATE agent_model_settings SET routes=?,version=version+1 WHERE id=1",
               (json.dumps(routes),))
    db.execute("INSERT INTO settings VALUES(?,?)", (marker, json.dumps(notes)))
    db.execute("UPDATE analysis_model SET version=version+1 WHERE id=1")


class ModelProviders:
    def __init__(self, store, credentials):
        self.store, self.credentials = store, credentials

    @staticmethod
    def settings(db):
        row = db.execute("SELECT * FROM agent_model_settings WHERE id=1").fetchone()
        return dict(row)

    @staticmethod
    def legacy(db):
        return db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()

    @classmethod
    def enabled(cls, db, settings):
        legacy = cls.legacy(db)
        return bool(legacy and json.loads(legacy["config"]).get("enabled"))

    @classmethod
    def resolve(cls, db, route):
        if route is None:
            row = cls.legacy(db)
            if not row:
                raise Conflict("analysis_model_required")
            return {"config": json.loads(row["config"]), "encrypted_key": row["encrypted_key"],
                    "provider_id": None, "provider_name": "原有默认配置",
                    "provider_version": row["version"]}
        row = db.execute("SELECT * FROM model_providers WHERE id=?",
                         (route["provider_id"],)).fetchone()
        if not row:
            raise Conflict("model_provider_missing")
        chosen = next((m for m in json.loads(row["models"]) if m["model"] == route["model"]), None)
        if not chosen:
            raise Conflict("model_provider_model_missing")
        return {"config": {**chosen, "base_url": row["base_url"], "api": row["api"]},
                "encrypted_key": row["encrypted_key"], "provider_id": row["id"],
                "provider_name": row["name"], "provider_version": row["version"]}

    def state(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            settings = self.settings(db)
            legacy = self.legacy(db)
            providers = [{"id": r["id"], "name": r["name"], "base_url": r["base_url"],
                          "api": r["api"], "models": json.loads(r["models"]),
                          "has_key": bool(r["encrypted_key"]), "version": r["version"]}
                         for r in db.execute("SELECT * FROM model_providers ORDER BY rowid")]
            notes = db.execute("SELECT value FROM settings WHERE key=?",
                               ("model_provider_five_roles_v1",)).fetchone()
            return {"version": settings["version"], "enabled": self.enabled(db, settings),
                    "routes": json.loads(settings["routes"]), "roles": AGENT_ROLES,
                    "providers": providers,
                    "legacy_available": bool(legacy and legacy["encrypted_key"]),
                    "legacy_model": json.loads(legacy["config"]).get("model", "") if legacy else "",
                    "migration_notes": json.loads(notes["value"]) if notes else []}

    @classmethod
    def role_rows(cls, db):
        """Resolve all roles in the caller's transaction, keeping credentials encrypted."""
        settings = cls.settings(db)
        routes = json.loads(settings["routes"])
        saved = {row["role"]: dict(row) for row in db.execute("SELECT * FROM agent_models")}
        rows = {}
        for role in ROLES:
            row = {"role": role, "config": "{}", "encrypted_key": "", "version": 0,
                   **saved.get(role, {}), "provider_id": None,
                   "provider_name": "角色独立配置", "provider_version": 0,
                   "selection_choice": routes.get(role), "routing_version": settings["version"]}
            choice = routes.get(role) or (routes.get("default") if role != COORDINATOR else None)
            if choice and not choice.get("direct"):
                try:
                    resolved = cls.resolve(db, choice)
                    row.update({key: resolved[key] for key in (
                        "encrypted_key", "provider_id", "provider_name", "provider_version",
                    )})
                    row["config"] = json.dumps(resolved["config"])
                except Conflict as exc:
                    row.update(encrypted_key="", selection_error=str(exc))
            rows[role] = row
        return rows

    @staticmethod
    def bump_revision(db, *, business=True):
        db.execute("UPDATE agent_model_settings SET version=version+1 WHERE id=1")
        if business:
            db.execute("INSERT INTO analysis_model VALUES(1,?, '',1) ON CONFLICT(id) "
                       "DO UPDATE SET version=analysis_model.version+1",
                       (json.dumps({"enabled": False}),))

    @staticmethod
    def touch_roles(db, roles):
        for role in roles:
            db.execute("INSERT INTO agent_models VALUES(?, '{}', '',1) ON CONFLICT(role) "
                       "DO UPDATE SET version=agent_models.version+1", (role,))

    @staticmethod
    def check_version(db, version):
        settings = ModelProviders.settings(db)
        if settings["version"] != version:
            raise Conflict("model_providers_changed")
        return settings

    @staticmethod
    def normalized(value):
        name = value["name"].strip()
        if not name or len(name) > 80 or not 1 <= len(value["models"]) <= 40:
            raise Conflict("model_provider_invalid")
        models = []
        base_url = value["base_url"]
        for item in value["models"]:
            try:
                config = validate_model_config(item["model"], base_url, value["api"],
                                               item.get("reasoning_effort", "auto"))
            except Conflict:
                raise Conflict("model_provider_invalid") from None
            base_url = config.pop("base_url")
            config.pop("api")
            models.append(config)
        if len({m["model"] for m in models}) != len(models):
            raise Conflict("model_provider_invalid")
        return {"name": name, "base_url": base_url, "api": value["api"], "models": models,
                "api_key": value.get("api_key", "").strip()}

    def _write(self, db, value, provider_id=None):
        old = db.execute("SELECT * FROM model_providers WHERE id=?", (provider_id,)).fetchone()
        if provider_id and not old:
            raise Missing("Model provider not found.")
        if not old and db.execute("SELECT count(*) FROM model_providers").fetchone()[0] >= 25:
            raise Conflict("model_provider_limit")
        if any(r["name"].casefold() == value["name"].casefold() for r in db.execute(
            "SELECT id,name FROM model_providers WHERE id != ?", (provider_id or "",),
        )):
            raise Conflict("model_provider_name_exists")
        if old and old["base_url"] != value["base_url"] and not value["api_key"]:
            raise Conflict("analysis_model_new_key_required")
        routes = json.loads(self.settings(db)["routes"])
        routes.update(self.coordination_routes(db))
        names = {m["model"] for m in value["models"]}
        if provider_id and any(r and r.get("provider_id") == provider_id
                               and r.get("model") not in names for r in routes.values()):
            raise Conflict("model_provider_in_use")
        if value["api_key"]:
            encrypted = self.credentials.cipher(db, create=True).encrypt(
                value["api_key"].encode(),
            ).decode()
        else:
            encrypted = old["encrypted_key"] if old else ""
        db.execute(
            "INSERT INTO model_providers VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name,base_url=excluded.base_url,api=excluded.api,models=excluded.models,"
            "encrypted_key=excluded.encrypted_key,version=excluded.version",
            (provider_id or uid("provider"), value["name"], value["base_url"], value["api"],
             json.dumps(value["models"]), encrypted, old["version"] + 1 if old else 1),
        )

    def save(self, *, version, provider_id=None, **value):
        normalized = self.normalized(value)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check_version(db, version)
            self._write(db, normalized, provider_id)
            self.bump_revision(db)
        return self.state()

    def import_providers(self, *, version, providers):
        if not 1 <= len(providers) <= 25:
            raise Conflict("model_provider_limit")
        values = [self.normalized(p) for p in providers]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check_version(db, version)
            for value in values:
                self._write(db, value)
            self.bump_revision(db)
        return self.state()

    def delete(self, provider_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.check_version(db, version)
            routes = {**json.loads(settings["routes"]), **self.coordination_routes(db)}
            if any(r and r.get("provider_id") == provider_id for r in routes.values()):
                raise Conflict("model_provider_in_use")
            if not db.execute("DELETE FROM model_providers WHERE id=?", (provider_id,)).rowcount:
                raise Missing("Model provider not found.")
            self.bump_revision(db)
        return self.state()

    @staticmethod
    def coordination_routes(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='coordination_configuration'").fetchone():
            return {}
        row = db.execute("SELECT fallback FROM coordination_configuration WHERE id=1").fetchone()
        return {"coordination_fallback": json.loads(row[0])} if row and row[0] else {}

    def configure(self, *, version, enabled, routes):
        if set(routes) - {"default", *AGENT_ROLES}:
            raise Conflict("model_agent_invalid")
        routes = {key: value for key, value in routes.items() if value is not None}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.check_version(db, version)
            old = json.loads(settings["routes"])
            if COORDINATOR not in routes and COORDINATOR in old:
                routes[COORDINATOR] = old[COORDINATOR]
            self._set_routes(db, settings, routes)
            legacy = self.legacy(db)
            config = json.loads(legacy["config"])
            config["enabled"] = enabled
            db.execute("UPDATE analysis_model SET config=? WHERE id=1", (json.dumps(config),))
            db.execute("UPDATE agent_model_settings SET enabled=? WHERE id=1", (int(enabled),))
        return self.state()

    @classmethod
    def _set_routes(cls, db, settings, routes):
        for key, route in routes.items():
            if route.get("direct"):
                if key == "default" or route != {"direct": True}:
                    raise Conflict("model_agent_invalid")
            elif not cls.resolve(db, route)["encrypted_key"]:
                raise Conflict("analysis_model_key_required")
        old = json.loads(settings["routes"])
        changed = [role for role in ROLES if old.get(role) != routes.get(role)
                   or role != COORDINATOR and not routes.get(role)
                   and old.get("default") != routes.get("default")]
        cls.touch_roles(db, changed)
        db.execute("UPDATE agent_model_settings SET routes=? WHERE id=1", (json.dumps(routes),))
        cls.bump_revision(db, business=any(role != COORDINATOR for role in changed)
                          or old.get("default") != routes.get("default"))

    def select(self, *, version, role, choice):
        if role not in (*ROLES, "default"):
            raise Conflict("model_agent_invalid")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.check_version(db, version)
            routes = json.loads(settings["routes"])
            if choice is None:
                routes.pop(role, None)
            else:
                routes[role] = choice
            self._set_routes(db, settings, routes)
        return self.state()

    @staticmethod
    def direct_changed(db, role):
        settings = ModelProviders.settings(db)
        routes = json.loads(settings["routes"])
        routes[role] = {"direct": True}
        db.execute("UPDATE agent_model_settings SET routes=?,version=version+1 WHERE id=1",
                   (json.dumps(routes),))

    @staticmethod
    def legacy_changed(db, enabled):
        routes = json.loads(ModelProviders.settings(db)["routes"])
        preserved = {COORDINATOR: routes[COORDINATOR]} if COORDINATOR in routes else {}
        db.execute("UPDATE agent_model_settings SET enabled=?,routes=?,version=version+1 "
                   "WHERE id=1", (int(enabled), json.dumps(preserved)))
