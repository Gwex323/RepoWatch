"""Source screening must not confuse declarations/references with credentials."""

import pytest

from repowatch.diagnosis import redact
from repowatch.source_safety import source_problem


@pytest.mark.parametrize("text", [
    'api_key: str = ""\n',
    'api_key: "str" = ""\n',
    'api_key: str | None = None\n',
    'api_key = settings.openai_api_key\n',
    'client = Client(api_key=settings.openai_api_key)\n',
    'client = Client(api_key=api_key)\n',
    'api_key = os.getenv("OPENAI_API_KEY", "")\n',
    'api_key: str = Field(default="", description="API key")\n',
    'def authenticate(password: str, token: str | None = None):\n    return token\n',
    'def authenticate(password: "str"):\n    return password\n',
    'result = {"access_token": access_token, "token_type": "bearer"}\n',
    'label = "中文"\nclient = Client(api_key=settings.api_key)\n',
])
def test_python_symbols_and_annotations_are_not_literal_credentials(text):
    assert redact(text) != text
    assert source_problem(text, "app.py") is None


@pytest.mark.parametrize("text", [
    'api_key = "private-literal-value"\n',
    'api_key: str = "private-literal-value"\n',
    'api_key: "str" = "private-literal-value"\n',
    'api_key: str = Field(default="private-literal-value")\n',
    'api_key: str = Field("private-literal-value")\n',
    'api_key = settings.api_key or "private-literal-value"\n',
    'api_key = settings.api_key if ready else "private-literal-value"\n',
    'api_key = str("private-literal-value")\n',
    'api_key = os.getenv("OPENAI_API_KEY", "private-literal-value")\n',
    'def authenticate(password: str = "private-literal-value"):\n    pass\n',
    'def authenticate(*, password: str = "private-literal-value"):\n    pass\n',
    'client = Client(api_key="private-literal-value")\n',
    'values = {"Authorization": "Bearer private-literal-value"}\n',
    'message = "password=private-literal-value"\n',
    '# password=private-literal-value\n',
    'value = "ghp_private_literal_value"\n',
    'value = "https://user:private-literal-value@example.com"\n',
    'api_key: str = "ghp_private_literal_value"\n',
    'label = "中文"; api_key: str = "private-literal-value"\n',
])
def test_real_literals_remain_blocked_without_echoing_content(text):
    problem = source_problem(text, "app.py")
    assert problem and problem["line"] >= 1
    assert set(problem) == {"line", "rule"}
    assert "private" not in str(problem)


@pytest.mark.parametrize("name,text", [
    ("app.ts", 'const api_key = "private-literal-value";'),
    ("app.go", 'var value = "ghp_private_literal_value"'),
    ("app.py", 'api_key: str = "private-literal-value"\ninvalid code!'),
    ("app.py", "value = '\0'"),
])
def test_unsupported_or_invalid_source_cannot_bypass_conservative_screening(name, text):
    assert source_problem(text, name)


def test_configured_secret_is_blocked_even_when_it_looks_like_a_reference(monkeypatch):
    monkeypatch.setenv("REPOWATCH_DIAGNOSIS_API_KEY", "private_configured_value")
    assert source_problem("api_key = private_configured_value\n", "app.py")


def test_log_redaction_remains_conservative():
    assert "private-literal-value" not in redact("password=private-literal-value")
    assert "[REDACTED]" in redact("api_key=settings.api_key")
