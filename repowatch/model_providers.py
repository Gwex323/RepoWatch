"""Saved provider catalog and owner-selected models; no network requests here."""

import json
from urllib.parse import urlsplit

from .store import Conflict, Missing, uid

REASONING_EFFORTS = {"auto", "default", "low", "medium", "high", "max"}
AGENT_ROLES = {
    "analysis.repository_analyst": "仓库分析 · 仓库分析员",
    "analysis.problem_investigator": "仓库分析 · 问题调查员",
    "analysis.reproduction_assistant": "仓库分析 · 复现辅助员",
    "analysis.independent_reviewer": "仓库分析 · 独立审查员",
    "repair.repair_agent": "自动修复 · 代码修复员",
    "repair.independent_reviewer": "自动修复 · 独立审查员",
    "diagnosis.repository_analyst": "运行诊断 · 原因分析员",
    "diagnosis.reproduction_assistant": "运行诊断 · 方案辅助员",
    "diagnosis.independent_reviewer": "运行诊断 · 独立审查员",
}


def model_for_role(model, role):
    return getattr(model, "role_models", {}).get(role, model)


def reasoning_for_role(model, role, default=None):
    return getattr(model_for_role(model, role), "reasoning_effort", default)


def validate_model_config(model, base_url, api, reasoning_effort="auto"):
    model, base_url = model.strip(), base_url.strip().rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (
            bool(url.hostname) and not url.username and not url.password
            and not url.query and not url.fragment
            and (url.port is None or 1 <= url.port <= 65535)
            and (url.scheme == "https" or url.scheme == "http"
                 and url.hostname in ("localhost", "127.0.0.1", "::1"))
        )
    except ValueError:
        valid = False
    if (not valid or not model or len(model) > 200 or len(base_url) > 500
            or api not in ("responses", "chat_completions")
            or reasoning_effort not in REASONING_EFFORTS
            or model.lower() == "glm-5.3" and reasoning_effort == "medium"):
        raise Conflict("analysis_model_invalid")
    return {"model": model, "base_url": base_url, "api": api,
            "reasoning_effort": reasoning_effort}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
            api TEXT NOT NULL, models TEXT NOT NULL, encrypted_key TEXT NOT NULL,
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_model_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
            enabled INTEGER, routes TEXT NOT NULL
        );
        INSERT OR IGNORE INTO agent_model_settings VALUES(1,0,NULL,'{}');
    """)


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
        return (bool(settings["enabled"]) if settings["enabled"] is not None
                else bool(legacy and json.loads(legacy["config"])["enabled"]))

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
            return {"version": settings["version"], "enabled": self.enabled(db, settings),
                    "routes": json.loads(settings["routes"]), "roles": AGENT_ROLES,
                    "providers": providers,
                    "legacy_available": bool(legacy and legacy["encrypted_key"]),
                    "legacy_model": json.loads(legacy["config"])["model"] if legacy else ""}

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
        names = {m["model"] for m in value["models"]}
        if any(r and r["provider_id"] == provider_id and r["model"] not in names
               for r in routes.values()):
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
            db.execute("UPDATE agent_model_settings SET version=version+1 WHERE id=1")
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
            db.execute("UPDATE agent_model_settings SET version=version+1 WHERE id=1")
        return self.state()

    def delete(self, provider_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.check_version(db, version)
            if any(r and r["provider_id"] == provider_id
                   for r in json.loads(settings["routes"]).values()):
                raise Conflict("model_provider_in_use")
            if not db.execute("DELETE FROM model_providers WHERE id=?", (provider_id,)).rowcount:
                raise Missing("Model provider not found.")
            db.execute("UPDATE agent_model_settings SET version=version+1 WHERE id=1")
        return self.state()

    def configure(self, *, version, enabled, routes):
        if set(routes) - {"default", *AGENT_ROLES}:
            raise Conflict("model_agent_invalid")
        routes = {key: value for key, value in routes.items() if value is not None}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check_version(db, version)
            for route in routes.values():
                if not self.resolve(db, route)["encrypted_key"]:
                    raise Conflict("analysis_model_key_required")
            if enabled and not self.resolve(db, routes.get("default"))["encrypted_key"]:
                raise Conflict("analysis_model_key_required")
            db.execute("UPDATE agent_model_settings SET enabled=?,routes=?,version=version+1 "
                       "WHERE id=1", (int(enabled), json.dumps(routes)))
        return self.state()

    @staticmethod
    def legacy_changed(db, enabled):
        settings = ModelProviders.settings(db)
        routes = json.loads(settings["routes"])
        routes.pop("default", None)
        db.execute("UPDATE agent_model_settings SET enabled=?,routes=?,version=version+1 "
                   "WHERE id=1", (int(enabled), json.dumps(routes)))
