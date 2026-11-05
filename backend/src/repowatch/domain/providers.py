"""供应商输入边界；错误不携带用户输入或凭据。"""

import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator

from repowatch.domain.protocols import Contract


class ProviderInputError(ValueError):
    def __init__(self, entry=None):
        self.entry = entry
        super().__init__("供应商输入无效" + (f"，条目 {entry}" if entry is not None else ""))


def validate_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError("地址不能含凭据、查询参数或片段")
    # Also validates malformed ports without echoing the original URL.
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("端口无效")
    return value.rstrip("/")


class ProviderModel(Contract):
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=30)

    @field_validator("model", "reasoning_effort")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("模型配置不能为空白")
        return value


class ProviderInput(Contract):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["chat_completions", "responses"]
    api_key: SecretStr | None = None
    models: tuple[ProviderModel, ...] = Field(default=(), max_length=100)
    chat_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    tool_choice: Literal["auto", "required"] = "auto"

    @field_validator("name")
    @classmethod
    def name_value(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("名称无效")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def url_value(cls, value):
        return validate_url(value)

    @field_validator("api_key")
    @classmethod
    def secret_value(cls, value):
        if value is not None and (
            not value.get_secret_value().strip()
            or len(value.get_secret_value().encode("utf-8")) > 2000
        ):
            raise ValueError("凭据无效")
        return value

    def public_configuration(self):
        return self.model_dump(mode="json", exclude={"api_key"})


def parse_providers(payload: str | dict | list) -> tuple[ProviderInput, ...]:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProviderInputError()
            result[key] = value
        return result

    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > 2 * 1024 * 1024:
            raise ProviderInputError()
        try:
            payload = json.loads(payload, object_pairs_hook=object_pairs)
        except (ValueError, RecursionError):
            raise ProviderInputError() from None
    if isinstance(payload, dict):
        if "providers" in payload:
            if set(payload) != {"providers"}:
                raise ProviderInputError()
            payload = payload["providers"]
        else:
            payload = [payload]
    if not isinstance(payload, list) or not 1 <= len(payload) <= 100:
        raise ProviderInputError()
    result, names = [], set()
    for index, entry in enumerate(payload, 1):
        try:
            provider = ProviderInput.model_validate(entry)
        except (ValidationError, ValueError, TypeError):
            raise ProviderInputError(index) from None
        normalized = provider.name.casefold()
        if normalized in names:
            raise ProviderInputError(index)
        names.add(normalized)
        result.append(provider)
    return tuple(result)
