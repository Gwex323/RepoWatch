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
from .store import Conflict

REQUEST_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10, write=30, pool=10)
REPAIR_REQUEST_TIMEOUT_SECONDS = 450
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


class AnalysisModel:
    def __init__(self, store, credentials):
        self.store, self.credentials = store, credentials

    @staticmethod
    def _legacy_status(row):
        if not row:
            return {"enabled": False, "model": "", "base_url": "https://api.openai.com/v1",
                    "api": "responses", "has_key": False, "version": 0,
                    "reasoning_effort": "auto", "effective_reasoning_effort": None,
                    "output_protocol": "json_schema", "finalization_mode": "provider_default"}
        config = json.loads(row["config"])
        return {**config, "reasoning_effort": config.get("reasoning_effort", "auto"),
                "effective_reasoning_effort": effective_reasoning(config),
                "output_protocol": output_protocol(config),
                "finalization_mode": finalization_mode(config),
                "has_key": bool(row["encrypted_key"]),
                "version": row["version"]}

    def legacy_status(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            row = ModelProviders.legacy(db)
            return {**self._legacy_status(row),
                    "routing_version": ModelProviders.settings(db)["version"]}

    def status(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            legacy = self._legacy_status(ModelProviders.legacy(db))
            settings = ModelProviders.settings(db)
            routes = json.loads(settings["routes"])
            selected = (ModelProviders.resolve(db, routes["default"])
                        if "default" in routes else None)
            config = selected["config"] if selected else legacy
            return {**legacy, **config, "enabled": ModelProviders.enabled(db, settings),
                    "has_key": bool(selected["encrypted_key"]) if selected else legacy["has_key"],
                    "effective_reasoning_effort": effective_reasoning(config),
                    "output_protocol": output_protocol(config),
                    "finalization_mode": finalization_mode(config),
                    "routing_version": settings["version"]}

    def save(self, *, version, enabled, model, base_url, api, api_key, reasoning_effort="auto",
             routing_version=None):
        config = {**validate_model_config(model, base_url, api, reasoning_effort),
                  "enabled": enabled}
        base_url, api_key = config["base_url"], api_key.strip()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if routing_version is not None:
                ModelProviders.check_version(db, routing_version)
            row = db.execute("SELECT * FROM analysis_model WHERE id=1").fetchone()
            if version != (row["version"] if row else 0):
                raise Conflict("analysis_model_changed")
            if row and json.loads(row["config"])["base_url"] != base_url and not api_key:
                raise Conflict("analysis_model_new_key_required")
            if api_key:
                cipher = self.credentials.cipher(db, create=True)
                encrypted = cipher.encrypt(api_key.encode()).decode()
            else:
                encrypted = row["encrypted_key"] if row else ""
            if enabled and not encrypted:
                raise Conflict("analysis_model_key_required")
            db.execute(
                "INSERT INTO analysis_model VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "config=excluded.config,encrypted_key=excluded.encrypted_key,"
                "version=excluded.version",
                (json.dumps(config), encrypted, version + 1),
            )
            ModelProviders.legacy_changed(db, enabled)
        return self.legacy_status()

    async def analyze(self, context, event):
        from .repository_analysis import run_analysis

        return await self._run(run_analysis, context, event)

    async def repair(self, context, event, *, workspace):
        from .repair_model import run_repair

        return await self._run(
            run_repair, context, event, workspace=workspace,
            workflow="repair",
            request_timeout_seconds=REPAIR_REQUEST_TIMEOUT_SECONDS,
        )

    async def diagnose(self, context):
        from .diagnosis import run_agents

        async def runner(model, value, _event, *, reasoning_effort=None):
            return await run_agents(model, value, reasoning_effort=reasoning_effort)

        return await self._run(runner, context, lambda *_: None, workflow="diagnosis")

    async def _run(self, runner, context, event, *,
                   request_timeout_seconds=REQUEST_TIMEOUT_SECONDS, workflow="analysis", **options):
        from .model_providers import AGENT_ROLES

        with self.store.connection() as db:
            db.execute("BEGIN")
            settings = ModelProviders.settings(db)
            if not ModelProviders.enabled(db, settings):
                raise Conflict("analysis_model_required")
            routes = json.loads(settings["routes"])
            selected = {"default": ModelProviders.resolve(db, routes.get("default"))}
            for key in AGENT_ROLES:
                if key.startswith(workflow + "."):
                    selected[key.split(".", 1)[1]] = ModelProviders.resolve(
                        db, routes.get(key, routes.get("default")),
                    )
            cipher = self.credentials.cipher(db)
            if cipher is None:
                raise Conflict("analysis_model_key_unavailable")
            try:
                secrets = {role: cipher.decrypt(item["encrypted_key"].encode()).decode()
                           for role, item in selected.items()}
            except (InvalidToken, UnicodeError):
                raise Conflict("analysis_model_key_unavailable") from None
            legacy = ModelProviders.legacy(db)
            legacy_version = legacy["version"] if legacy else 0
        config = selected["default"]["config"]
        models, opened, identities = {}, {}, {}
        async with AsyncExitStack() as stack:
            for role, item in selected.items():
                value = item["config"]
                cache_key = (item["provider_id"], value["model"])
                if cache_key not in opened:
                    observation = {}

                    async def observe(response, observation=observation):
                        await observe_response(response, observation, event)

                    # Each endpoint gets its own key; redirects/proxies cannot forward it.
                    client = await stack.enter_async_context(AsyncOpenAI(
                        api_key=secrets[role], base_url=value["base_url"], max_retries=0,
                        timeout=httpx.Timeout(request_timeout_seconds,
                                              connect=10, write=30, pool=10),
                        http_client=httpx.AsyncClient(
                            trust_env=False, follow_redirects=False,
                            event_hooks={"response": [observe]},
                        ),
                    ))
                    protocol = output_protocol(value)
                    if deepseek_chat_compatibility(value):
                        event("provider_compatibility", "deepseek_chat")
                        delegate = DeepSeekChatModel(value["model"], client)
                    elif protocol == "json_object":
                        delegate = JsonObjectChatModel(
                            value["model"], client,
                            fast_finalization=finalization_mode(value) == "glm47_direct",
                            event=event,
                        )
                    else:
                        adapter = (OpenAIResponsesModel if value["api"] == "responses"
                                   else OpenAIChatCompletionsModel)
                        delegate = adapter(value["model"], client)
                    observed = ObservedModel(
                        delegate, observation,
                        separate_schema_from_tools=separate_review_schema_from_tools(value),
                        retry_handoff_mismatch=deepseek_chat_compatibility(value),
                    )
                    observed.reasoning_effort = effective_reasoning(value)
                    opened[cache_key] = observed
                models[role] = opened[cache_key]
                if role != "default":
                    identities[role] = {key: item[key] for key in (
                        "provider_id", "provider_name", "provider_version",
                    )}
                    identities[role].update(value)
                    identities[role].pop("enabled", None)
                    identities[role]["effective_reasoning_effort"] = effective_reasoning(value)
                    event("agent_model", json.dumps({"role": role, **identities[role]},
                                                    ensure_ascii=False))
            model = models["default"]
            model.role_models = {role: value for role, value in models.items() if role != "default"}
            event("output_protocol", output_protocol(config))
            result = await runner(
                model, context, event, reasoning_effort=effective_reasoning(config), **options,
            )
        # Redact every selected key, including a credential echoed by another provider.
        secret_values = sorted(set(secrets.values()), key=len, reverse=True)

        def redact_credentials(value):
            if isinstance(value, str):
                for secret in secret_values:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, list):
                return [redact_credentials(item) for item in value]
            if isinstance(value, dict):
                return {key: redact_credentials(item) for key, item in value.items()}
            return value

        result = redact_credentials(json.loads(json.dumps(result)))
        result["model"] = config["model"]
        result["model_version"] = legacy_version
        result["reasoning_effort"] = effective_reasoning(config)
        result["routing_version"] = settings["version"]
        result["agent_models"] = identities
        return result
