"""Explicit, bounded provider checks; never save configuration or replay requests."""

import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken

from .model_providers import ModelProviders, validate_model_config
from .store import Conflict

MODEL_LIST_TIMEOUT = 20
CONNECTION_TIMEOUT = 45
MAX_RESPONSE_BYTES = 512000
MAX_DISCOVERED_MODELS = 1000
PROBE_OUTPUT_TOKENS = 32


class ProviderProbe:
    def __init__(self, providers, *, transport=None):
        self.providers, self.transport = providers, transport

    def configuration(self, *, version, base_url, api, api_key, provider_id=None, model=""):
        config = validate_model_config(model or "provider-probe", base_url, api)
        if any(ord(c) < 33 or ord(c) == 127 for c in config["base_url"]):
            raise Conflict("analysis_model_invalid")
        secret = api_key.strip()
        with self.providers.store.connection() as db:
            db.execute("BEGIN")
            ModelProviders.check_version(db, version)
            old = None
            if provider_id:
                old = db.execute("SELECT * FROM model_providers WHERE id=?",
                                 (provider_id,)).fetchone()
                if not old:
                    raise Conflict("model_provider_missing")
                if old["base_url"] != config["base_url"] and not secret:
                    raise Conflict("analysis_model_new_key_required")
            if not secret:
                if not old or not old["encrypted_key"]:
                    raise Conflict("analysis_model_key_required")
                cipher = self.providers.credentials.cipher(db)
                if cipher is None:
                    raise Conflict("analysis_model_key_unavailable")
                try:
                    secret = cipher.decrypt(old["encrypted_key"].encode()).decode()
                except (InvalidToken, UnicodeError):
                    raise Conflict("analysis_model_key_unavailable") from None
        if not secret.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            raise Conflict("model_probe_key_invalid")
        return config, secret

    async def request(self, config, secret, operation, payload=None):
        duration = MODEL_LIST_TIMEOUT if operation == "models" else CONNECTION_TIMEOUT

        async def perform():
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, transport=self.transport,
                timeout=httpx.Timeout(duration, connect=10, write=10, pool=10),
            ) as client:
                async with client.stream(
                    "GET" if payload is None else "POST",
                    config["base_url"] + "/" + operation,
                    headers={"Authorization": "Bearer " + secret}, json=payload,
                ) as response:
                    code = response.status_code
                    if code >= 300:
                        reason = {
                            401: "authentication", 403: "forbidden", 429: "rate_limited",
                            400: "request_rejected", 422: "request_rejected",
                            404: "models_unsupported" if operation == "models" else "not_found",
                            405: "models_unsupported" if operation == "models" else "not_found",
                        }.get(code, "redirect" if code < 400 else "service_error")
                        raise Conflict("model_probe_" + reason)
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise Conflict("model_probe_response_limit")
                    try:
                        result = json.loads(content)
                    except (ValueError, UnicodeError, RecursionError):
                        raise Conflict("model_probe_invalid_response") from None
                    if not isinstance(result, dict):
                        raise Conflict("model_probe_invalid_response")
                    return result

        try:
            return await asyncio.wait_for(perform(), timeout=duration)
        except (TimeoutError, httpx.TimeoutException):
            raise Conflict("model_probe_timeout") from None
        except httpx.HTTPError:
            raise Conflict("model_probe_unreachable") from None
        except httpx.InvalidURL:
            raise Conflict("analysis_model_invalid") from None

    async def discover(self, **values):
        values["model"] = ""
        config, secret = self.configuration(**values)
        response = await self.request(config, secret, "models")
        records = response.get("data")
        if not isinstance(records, list):
            raise Conflict("model_probe_invalid_response")
        models = sorted({
            item["id"].strip() for item in records
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and 0 < len(item["id"].strip()) <= 200
            and not any(ord(c) < 32 or ord(c) == 127 for c in item["id"])
            and secret not in item["id"]
        })
        return {"models": models[:MAX_DISCOVERED_MODELS],
                "truncated": len(models) > MAX_DISCOVERED_MODELS or bool(response.get("has_more"))}

    async def test_connection(self, **values):
        if not values.get("model", "").strip():
            raise Conflict("model_probe_model_required")
        config, secret = self.configuration(**values)
        if config["api"] == "responses":
            operation = "responses"
            payload = {"model": config["model"], "input": "Reply with OK.",
                       "max_output_tokens": PROBE_OUTPUT_TOKENS, "store": False, "stream": False}
        else:
            operation = "chat/completions"
            token_field = ("max_completion_tokens"
                           if urlsplit(config["base_url"]).hostname == "api.openai.com"
                           else "max_tokens")
            payload = {"model": config["model"], "messages": [{"role": "user",
                       "content": "Reply with OK."}], token_field: PROBE_OUTPUT_TOKENS,
                       "stream": False}
        started = time.monotonic()
        response = await self.request(config, secret, operation, payload)
        if config["api"] == "responses":
            valid = (response.get("object") == "response"
                     and response.get("status") in ("completed", "incomplete")
                     and isinstance(response.get("output"), list))
        else:
            choices = response.get("choices")
            valid = bool(isinstance(choices, list) and choices and isinstance(choices[0], dict)
                         and isinstance(choices[0].get("message"), dict)
                         and choices[0]["message"].get("role") == "assistant")
        if not valid:
            raise Conflict("model_probe_invalid_response")
        return {"connected": True, "latency_ms": round((time.monotonic() - started) * 1000)}
