"""配置、事件、错误和日志统一去敏；不记录 SDK 的原始错误正文。"""

import logging
import re
from urllib.parse import quote, quote_plus

from repowatch.application.credentials import SecretValue

PRIVATE_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|authorization|cookie|access[_-]?token|"
    r"refresh[_-]?token|session[_-]?(?:id|token))",
    re.IGNORECASE,
)
INLINE_SECRET = re.compile(
    r"(?i)(bearer\s+)[^\s,;\"']+|"
    r"((?:api[_-]?key|password|secret|access_token|session_token)\s*[=:]\s*)[^\s,;\"']+",
)


class Redactor:
    def __init__(self):
        self._secrets: set[str] = set()

    def register(self, secret: SecretValue):
        value = secret.reveal()
        self._secrets.update((value, quote(value, safe=""), quote_plus(value)))

    def text(self, value: str):
        result = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            result = result.replace(secret, "[REDACTED]")
        return INLINE_SECRET.sub(lambda match: (match[1] or match[2]) + "[REDACTED]", result)

    def clean(self, value):
        if isinstance(value, SecretValue):
            return "[REDACTED]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                self.text(str(key)): "[REDACTED]"
                if PRIVATE_KEY.search(str(key))
                else self.clean(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if value is None or type(value) in {bool, int, float}:
            return value
        return "[REDACTED]"


class RedactingLogFilter(logging.Filter):
    def __init__(self, redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = "日志参数无效"
        record.msg = self.redactor.text(message)
        record.args = ()
        if record.exc_info:
            record.msg += " [异常详情已隐藏]"
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        # Custom log formatters may use structured extra attributes.
        for key, value in list(record.__dict__.items()):
            if key not in {"msg", "args", "exc_info", "exc_text", "stack_info"}:
                record.__dict__[key] = (
                    "[REDACTED]" if PRIVATE_KEY.search(key) else self.redactor.clean(value)
                )
        return True


DEFAULT_REDACTOR = Redactor()


def install_log_redaction():
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "openai"):
        logger = logging.getLogger(name)
        targets = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(item, RedactingLogFilter) for item in target.filters):
                target.addFilter(RedactingLogFilter(DEFAULT_REDACTOR))
