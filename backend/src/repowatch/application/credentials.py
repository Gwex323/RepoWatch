"""仅宿主供应商适配持有的凭据端口，不作为 Agent 工具暴露。"""

import re
from typing import Protocol


class CredentialError(RuntimeError):
    def __init__(self):
        super().__init__("宿主受保护凭据不可用，请核对配置引用")


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 2000:
            raise ValueError("凭据不能为空且最多 2000 字节")
        self._value = value

    def __str__(self):
        return "[REDACTED]"

    def __repr__(self):
        return "SecretValue([REDACTED])"

    def reveal(self) -> str:
        return self._value


def validate_reference(reference):
    if not isinstance(reference, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", reference):
        raise ValueError("凭据引用格式无效")


class CredentialVault(Protocol):
    def create(self, reference: str, secret: SecretValue) -> None: ...

    def read(self, reference: str) -> SecretValue: ...

    def delete(self, reference: str) -> None: ...
