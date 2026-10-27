"""Owner-configured analysis provider; credentials never enter repository containers."""

import json
from contextlib import AsyncExitStack
from dataclasses import replace
from urllib.parse import urlsplit

import httpx
from agents import FunctionTool, Model, OpenAIChatCompletionsModel, OpenAIResponsesModel
from agents.exceptions import ModelBehaviorError
from cryptography.fernet import InvalidToken
from openai import AsyncOpenAI

from .model_providers import ModelProviders, validate_model_config
from .report_recovery import digest
from .roles import FLOW_ROLES, ROLE_DETAILS, ROLES
from .store import Conflict

REQUEST_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10, write=30, pool=10)
REPAIR_REQUEST_TIMEOUT_SECONDS = 450
REASONING_EFFORTS = {"auto", "default", "low", "medium", "high", "max"}
PROVIDER_REQUEST_FIELDS = {
    "tools", "tool_choice", "response_format", "parallel_tool_calls",
    "reasoning_effort", "max_tokens",
}
PROVIDER_REQUEST_CODES = {
    "BadRequest", "InvalidParameter", "InvalidParameterValue", "InvalidRequest",
    "UnsupportedParameter",
}


def deepseek_chat_compatibility(config):
    url = urlsplit(config.get("base_url", ""))
    return (config.get("api") == "chat_completions"
            and url.scheme == "https" and url.hostname == "api.deepseek.com"
            and url.port in (None, 443) and url.path.rstrip("/") in ("", "/v1", "/beta"))


def effective_reasoning(config):
    if deepseek_chat_compatibility(config):
        return None
    effort = config.get("reasoning_effort", "auto")
    if effort == "auto":
        return "low" if config["model"].lower() in {"glm-5.3", "glm-5.3-flash"} else None
    return None if effort == "default" else effort


def output_protocol(config):
    if deepseek_chat_compatibility(config) or separate_review_schema_from_tools(config):
        return "json_object"
    if (config["api"] == "chat_completions" and config["model"].lower() == "glm-4.7"
            and urlsplit(config["base_url"]).hostname in ("open.bigmodel.cn", "api.z.ai")):
        return "json_object"
    return "json_schema"


def separate_review_schema_from_tools(config):
    url = urlsplit(config["base_url"])
    return (config["api"] == "chat_completions"
            and config["model"].lower() in {"glm-5.3", "glm-5.3-flash"}
            and url.scheme == "https" and url.port in (None, 443)
            and url.hostname == "ark.cn-beijing.volces.com"
            and url.path.rstrip("/") == "/api/coding/v3")


def finalization_mode(config):
    if deepseek_chat_compatibility(config):
        return "deepseek_direct"
    if separate_review_schema_from_tools(config):
        return "provider_default"
    if (output_protocol(config) == "json_object"
            and config.get("reasoning_effort", "auto") == "auto"):
        return "glm47_direct"
    return "provider_default"


class JsonObjectChatModel(OpenAIChatCompletionsModel):
    """Use provider JSON mode while retaining the SDK's strict output schema."""

    def __init__(self, model, client, *, fast_finalization=False, event=lambda *_: None):
        super().__init__(model, client)
        self.fast_finalization, self.event = fast_finalization, event

    async def get_response(self, *args, **kwargs):
        schema = kwargs.get("output_schema")
        if schema is not None and not schema.is_plain_text():
            # JSON mode enforces syntax only. Preserve the model's knowledge of
            # the output contract when removing its wire-level JSON Schema.
            contract = "\nReturn one JSON object matching this schema:\n" + json.dumps(
                schema.json_schema(), ensure_ascii=False,
            )
            if args:
                args = ((args[0] or "") + contract, *args[1:])
            else:
                kwargs["system_instructions"] = (kwargs.get("system_instructions") or "") + contract
            settings = kwargs["model_settings"]
            if (self.fast_finalization and not kwargs.get("tools") and not kwargs.get("handoffs")
                    and settings.tool_choice == "none" and settings.reasoning is None
                    and "thinking" not in (settings.extra_body or {})):
                settings = replace(
                    settings, extra_body={**(settings.extra_body or {}),
                                          "thinking": {"type": "disabled"}},
                )
                self.event("finalization_thinking", "disabled")
            kwargs["model_settings"] = replace(
                settings, extra_args={**(settings.extra_args or {}),
                                      "response_format": {"type": "json_object"}},
            )
            # Let the provider enforce JSON syntax while the runner performs the
            # canonical Pydantic validation after receiving the text.
            kwargs["output_schema"] = None
        return await super().get_response(*args, **kwargs)


class DeepSeekChatModel(JsonObjectChatModel):
    """Keep native forced tools usable on the official non-thinking chat API."""

    async def get_response(self, *args, **kwargs):
        settings = kwargs["model_settings"]
        # Every stage may reach a forced tool/handoff. Keeping the whole run in
        # non-thinking mode also avoids switching reasoning history mid-run.
        kwargs["model_settings"] = replace(
            settings, reasoning=None,
            extra_body={**(settings.extra_body or {}), "thinking": {"type": "disabled"}},
        )
        # Strict tool schemas are a separate beta feature. Keep the original
        # SDK tools and local argument validation; change only their wire flags.
        kwargs["tools"] = [
            replace(tool, strict_json_schema=False) if isinstance(tool, FunctionTool) else tool
            for tool in kwargs["tools"]
        ]
        kwargs["handoffs"] = [
            replace(handoff, strict_json_schema=False) for handoff in kwargs["handoffs"]
        ]
        return await super().get_response(*args, **kwargs)


class OutputTruncated(ModelBehaviorError):
    """The provider explicitly ended generation at its output-token limit."""


class ObservedModel(Model):
    """Observe safe completion metadata before accepting SDK-decoded output."""

    def __init__(self, delegate, observation, *, separate_schema_from_tools=False,
                 retry_handoff_mismatch=False):
        self.delegate, self.observation = delegate, observation
        self.separate_schema_from_tools = separate_schema_from_tools
        self.retry_handoff_mismatch = retry_handoff_mismatch

    async def get_response(self, *args, **kwargs):
        self.observation.clear()
        try:
            result = await self.delegate.get_response(*args, **kwargs)
        except ModelBehaviorError:
            if self.observation.get("truncated"):
                raise OutputTruncated("analysis_output_truncated") from None
            raise
        if self.observation.get("truncated"):
            raise OutputTruncated("analysis_output_truncated")
        return result

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Repository analysis uses non-streaming calls.")
        yield


async def observe_response(response, observation, event):
    """Keep only safe metadata, never provider prose, model text or tool arguments."""
    if not response.is_success:
        if response.status_code in (400, 422):
            await response.aread()
            details = {"http_status": response.status_code}
            try:
                body = response.json()
            except ValueError:
                body = None
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(code, str) and code in PROVIDER_REQUEST_CODES:
                    details["provider_code"] = code
                param = error.get("param")
                if isinstance(param, str):
                    field = param.split(".", 1)[0].split("[", 1)[0]
                    if field in PROVIDER_REQUEST_FIELDS:
                        details["parameter"] = field
            event("provider_rejection", json.dumps(details))
        return
    await response.aread()
    try:
        body = response.json()
    except ValueError:
        event("provider_response", json.dumps({"reason": "non_json"}))
        return
    if not isinstance(body, dict):
        event("provider_response", json.dumps({"reason": "invalid_envelope"}))
        return
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(first, dict):
        first = {}
    finish = first.get("finish_reason")
    allowed = {"stop", "length", "tool_calls", "function_call", "content_filter"}
    finish = finish if isinstance(finish, str) and finish in allowed else "unknown"
    incomplete = body.get("incomplete_details")
    truncated = finish == "length" or (
        body.get("status") == "incomplete" and isinstance(incomplete, dict)
        and incomplete.get("reason") == "max_output_tokens"
    )
    observation["truncated"] = truncated
    details = {"finish_reason": finish, "truncated": truncated}
    usage = body.get("usage")
    if isinstance(usage, dict):
        tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        token_details = usage.get("completion_tokens_details", usage.get("output_tokens_details"))
        if type(tokens) is int and 0 <= tokens <= 10000000:
            details["output_tokens"] = tokens
        if isinstance(token_details, dict):
            reasoning = token_details.get("reasoning_tokens")
            if type(reasoning) is int and 0 <= reasoning <= 10000000:
                details["reasoning_tokens"] = reasoning
    event("provider_response", json.dumps(details))


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS analysis_model (
            id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL,
            encrypted_key TEXT NOT NULL, version INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_models (
            role TEXT PRIMARY KEY, config TEXT NOT NULL,
            encrypted_key TEXT NOT NULL, version INTEGER NOT NULL
        )
    """)
    legacy = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
    if legacy:
        config = json.loads(legacy["config"])
        if config.get("model"):
            config.pop("enabled", None)
            for role in ROLES:
                db.execute("INSERT OR IGNORE INTO agent_models VALUES(?,?,?,?)",
                           (role, json.dumps(config), legacy["encrypted_key"], legacy["version"]))


def default_config():
    return {"model": "", "base_url": "https://api.openai.com/v1", "api": "responses",
            "reasoning_effort": "auto"}


def validate_config(model, base_url, api, reasoning_effort):
    return validate_model_config(model, base_url, api, reasoning_effort)


def public_model(role, row):
    config = {**default_config(), **(json.loads(row["config"]) if row else {})}
    config.pop("enabled", None)
    return {**config, "role": role, **ROLE_DETAILS[role],
            "effective_reasoning_effort": effective_reasoning(config),
            "output_protocol": output_protocol(config),
            "finalization_mode": finalization_mode(config),
            "has_key": bool(row and row["encrypted_key"]),
            "configured": bool(config["model"] and row and row["encrypted_key"]),
            **{key: row[key] for key in ("provider_id", "provider_name", "provider_version",
                                         "selection_choice", "routing_version", "selection_error")
               if row and key in row},
            "version": row["version"] if row else 0}


class ModelFlow:
    """Expose readiness for the roles needed by one durable queue."""

    def __init__(self, parent, flow):
        self.parent, self.flow = parent, flow

    def status(self):
        return self.parent.status(self.flow)

    async def repair(self, *args, **kwargs):
        return await self.parent.repair(*args, **kwargs)


class AnalysisModel:
    def __init__(self, store, credentials):
        self.store, self.credentials = store, credentials

    def _snapshot(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            roles = ModelProviders.role_rows(db)
            direct = {item["role"]: dict(item) for item in db.execute("SELECT * FROM agent_models")}
        return dict(row) if row else None, roles, direct

    @staticmethod
    def _identity(row, rows):
        # Hash encrypted credentials as well: even a key-only replacement must
        # invalidate recovery. Neither keys nor encrypted values leave this hash.
        return digest({"settings": dict(row) if row else None,
                       "roles": {role: dict(rows[role]) if role in rows else None
                                 for role in FLOW_ROLES["analysis"]}})

    def recovery_identity(self):
        row, rows, _ = self._snapshot()
        return self._identity(row, rows)

    def configuration(self):
        row, rows, direct = self._snapshot()
        roles = {role: {**public_model(role, rows.get(role)),
                        "direct_config": public_model(role, direct.get(role))} for role in ROLES}
        enabled = bool(row and json.loads(row["config"]).get("enabled"))
        return {"enabled": enabled, "version": row["version"] if row else 0,
                "roles": list(roles.values()),
                "flows": {flow: {"ready": all(roles[r]["configured"] for r in required),
                                 "missing_roles": [r for r in required
                                                   if not roles[r]["configured"]]}
                          for flow, required in FLOW_ROLES.items()}}

    def status(self, flow="analysis"):
        config = self.configuration()
        required = FLOW_ROLES[flow]
        selected = [role for role in config["roles"] if role["role"] in required]
        first = selected[0]
        names = {role["model"] for role in selected if role["model"]}
        return {**first, "enabled": config["enabled"] and config["flows"][flow]["ready"],
                "configured_enabled": config["enabled"],
                "model": next(iter(names)) if len(names) == 1 else "按角色配置" if names else "",
                "has_key": all(role["has_key"] for role in selected),
                "version": config["version"],
                "missing_roles": config["flows"][flow]["missing_roles"],
                "role_models": {
                    role["role"]: {key: role[key] for key in
                                   ("model", "base_url", "api", "version", "reasoning_effort",
                                    "provider_id", "provider_name", "provider_version")}
                    for role in selected
                }}

    def for_flow(self, flow):
        return ModelFlow(self, flow)

    @staticmethod
    def _encrypted_key(db, credentials, row, config, api_key):
        if (row and json.loads(row["config"]).get("base_url") != config["base_url"]
                and not api_key):
            raise Conflict("analysis_model_new_key_required")
        if api_key:
            cipher = credentials.cipher(db, create=True)
            return cipher.encrypt(api_key.encode()).decode()
        return row["encrypted_key"] if row else ""

    @staticmethod
    def _bump_version(db, row, *, enabled=None):
        config = json.loads(row["config"]) if row else {}
        if enabled is not None:
            config["enabled"] = enabled
        db.execute(
            "INSERT INTO analysis_model VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "config=excluded.config,version=excluded.version",
            (json.dumps(config), "", row["version"] + 1 if row else 1),
        )

    def enable(self, *, version, enabled):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            if version != (row["version"] if row else 0):
                raise Conflict("analysis_model_changed")
            self._bump_version(db, row, enabled=enabled)
            db.execute("UPDATE agent_model_settings SET enabled=?,version=version+1 WHERE id=1",
                       (int(enabled),))
        return self.configuration()

    def save_role(self, role, *, version, model, base_url, api, api_key, reasoning_effort="auto"):
        if role not in ROLES:
            raise Conflict("analysis_model_role_invalid")
        config = validate_config(model, base_url, api, reasoning_effort)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM agent_models WHERE role=?", (role,)).fetchone()
            if version != (row["version"] if row else 0):
                raise Conflict("analysis_model_changed")
            encrypted = self._encrypted_key(db, self.credentials, row, config, api_key.strip())
            if not encrypted:
                raise Conflict("analysis_model_key_required")
            db.execute(
                "INSERT INTO agent_models VALUES(?,?,?,?) ON CONFLICT(role) DO UPDATE SET "
                "config=excluded.config,encrypted_key=excluded.encrypted_key,"
                "version=excluded.version",
                (role, json.dumps(config), encrypted, version + 1),
            )
            global_row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            self._bump_version(db, global_row)
            ModelProviders.direct_changed(db, role)
        return self.configuration()

    def save(self, *, version, enabled, model, base_url, api, api_key, reasoning_effort="auto"):
        """Retain the legacy API as an explicit update of all five roles."""
        config = {**validate_config(model, base_url, api, reasoning_effort), "enabled": enabled}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            if version != (row["version"] if row else 0):
                raise Conflict("analysis_model_changed")
            key_row = db.execute(
                "SELECT * FROM agent_models WHERE role=?", (ROLES[0],),
            ).fetchone()
            encrypted = self._encrypted_key(db, self.credentials, key_row, config, api_key.strip())
            if enabled and not encrypted:
                raise Conflict("analysis_model_key_required")
            db.execute(
                "INSERT INTO analysis_model VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "config=excluded.config,encrypted_key=excluded.encrypted_key,"
                "version=excluded.version",
                (json.dumps(config), encrypted, version + 1),
            )
            role_config = {key: value for key, value in config.items() if key != "enabled"}
            for role in ROLES:
                db.execute(
                    "INSERT INTO agent_models VALUES(?,?,?,1) ON CONFLICT(role) DO UPDATE SET "
                    "config=excluded.config,encrypted_key=excluded.encrypted_key,"
                    "version=agent_models.version+1",
                    (role, json.dumps(role_config), encrypted),
                )
            ModelProviders.legacy_changed(db, enabled)
        return self.status()

    async def analyze(self, context, event):
        from .repository_analysis import run_analysis

        return await self._run(run_analysis, context, event, flow="analysis")

    async def repair(self, context, event, *, workspace):
        from .repair_model import run_repair

        return await self._run(
            run_repair, context, event, workspace=workspace, flow="repair",
            request_timeout_seconds=REPAIR_REQUEST_TIMEOUT_SECONDS,
        )

    async def diagnose(self, context):
        from .diagnosis import run_agents

        async def runner(model, value, _event, *, reasoning_effort=None):
            return await run_agents(model, value, reasoning_effort=reasoning_effort)

        return await self._run(runner, context, lambda *_: None, flow="diagnosis")

    async def _run(self, runner, context, event, *, flow,
                   request_timeout_seconds=REQUEST_TIMEOUT_SECONDS, **options):
        required = FLOW_ROLES[flow]
        with self.store.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            rows = ModelProviders.role_rows(db)
            if (not row or not json.loads(row["config"]).get("enabled")
                    or any(role not in rows or not public_model(role, rows[role])["configured"]
                           for role in required)):
                raise Conflict("analysis_model_required")
            cipher = self.credentials.cipher(db)
            if cipher is None:
                raise Conflict("analysis_model_key_unavailable")
            try:
                secrets = {role: cipher.decrypt(rows[role]["encrypted_key"].encode()).decode()
                           for role in required}
            except (InvalidToken, UnicodeError):
                raise Conflict("analysis_model_key_unavailable") from None
        configs = {role: json.loads(rows[role]["config"]) for role in required}
        if (flow == "analysis" and context.get("_review_model_identity") is not None
                and context["_review_model_identity"] != self._identity(row, rows)):
            raise Conflict("analysis_model_changed")
        original_event = event

        def event(kind, value):
            if kind == "review_checkpoint":
                for secret in sorted(set(secrets.values()), key=len, reverse=True):
                    value = value.replace(json.dumps(secret, ensure_ascii=False)[1:-1],
                                          "[REDACTED]")
            return original_event(kind, value)

        models, efforts = {}, {}
        first = required[0]
        uniform = all(configs[role] == configs[first] and secrets[role] == secrets[first]
                      for role in required)
        if uniform:
            event("output_protocol", output_protocol(configs[first]))
            if deepseek_chat_compatibility(configs[first]):
                event("provider_compatibility", "deepseek_chat")
        # Each role owns its transport, credentials and compatibility settings for this run.
        async with AsyncExitStack() as stack:
            for role in required:
                config, observation = configs[role], {}
                efforts[role] = effective_reasoning(config)
                protocol = output_protocol(config)
                event("model_configuration", json.dumps({
                    "role": role, "name": ROLE_DETAILS[role]["name"],
                    "model": config["model"], "output_protocol": protocol,
                    "provider_id": rows[role]["provider_id"],
                    "provider_name": rows[role]["provider_name"],
                    "provider_version": rows[role]["provider_version"],
                    "reasoning_effort": efforts[role],
                    "finalization_mode": finalization_mode(config),
                }))
                if uniform and role != first:
                    models[role] = models[first]
                    continue

                async def observe(response, observation=observation):
                    await observe_response(response, observation, event)

                client = await stack.enter_async_context(AsyncOpenAI(
                    api_key=secrets[role], base_url=config["base_url"], max_retries=0,
                    timeout=httpx.Timeout(request_timeout_seconds, connect=10, write=30, pool=10),
                    http_client=httpx.AsyncClient(
                        trust_env=False, follow_redirects=False,
                        event_hooks={"response": [observe]},
                    ),
                ))
                if deepseek_chat_compatibility(config):
                    delegate = DeepSeekChatModel(config["model"], client)
                elif protocol == "json_object":
                    delegate = JsonObjectChatModel(
                        config["model"], client,
                        fast_finalization=finalization_mode(config) == "glm47_direct", event=event,
                    )
                else:
                    adapter = (
                        OpenAIResponsesModel if config["api"] == "responses"
                        else OpenAIChatCompletionsModel
                    )
                    delegate = adapter(config["model"], client)
                models[role] = ObservedModel(
                    delegate, observation,
                    separate_schema_from_tools=separate_review_schema_from_tools(config),
                    retry_handoff_mismatch=deepseek_chat_compatibility(config),
                )
            result = await runner(
                models[first] if uniform else models, context, event,
                reasoning_effort=efforts[first] if uniform else efforts,
                **options,
            )
        # The key is not part of the prompt, but never persist a provider-echoed credential.
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in sorted(set(secrets.values()), key=len, reverse=True):
            serialized = serialized.replace(
                json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]",
            )
        result = json.loads(serialized)
        names = {config["model"] for config in configs.values()}
        result["model"] = next(iter(names)) if len(names) == 1 else "按角色配置"
        result["model_version"] = row["version"]
        result["reasoning_effort"] = (next(iter(set(efforts.values())))
                                      if len(set(efforts.values())) == 1 else "per_role")
        result["role_models"] = {
            role: {"model": configs[role]["model"], "model_version": rows[role]["version"],
                   "reasoning_effort": efforts[role],
                   "provider_id": rows[role]["provider_id"],
                   "provider_name": rows[role]["provider_name"],
                   "provider_version": rows[role]["provider_version"]}
            for role in required
        }
        return result
