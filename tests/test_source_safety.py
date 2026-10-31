"""Source screening must not confuse declarations/references with credentials."""

import pytest

from repowatch.diagnosis import redact, redact_known
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


def test_source_screening_distinguishes_short_sk_labels_from_real_keys():
    for text in (
        "model = 'volcengine-plan//sk-only'\n",
        "sk = 'sk-fallback-key'\n",
        "label = 'task-only'\n",
    ):
        assert source_problem(text, "app.py") is None

    synthetic_key = "sk-" + "A1b2" * 8
    problem = source_problem(f"value = '{synthetic_key}'\n", "app.py")
    assert problem == {"line": 1, "rule": "credential_pattern"}


@pytest.mark.parametrize("length,blocked", [(19, False), (20, True), (21, True)])
def test_source_sk_key_shape_has_an_explicit_length_boundary(length, blocked):
    body = ("A1b2" * ((length + 3) // 4))[:length]
    problem = source_problem(f"value = 'sk-{body}'\n", "app.py")
    assert bool(problem) is blocked
    assert problem is None or problem["rule"] == "credential_pattern"


def test_short_sk_labels_are_allowed_in_comments_and_non_python_sources():
    for name, text in (
        ("app.py", "# placeholder: sk-fallback-key\n"),
        ("model.json", '{"name": "volcengine-plan//sk-only"}\n'),
        ("README.md", "Model: volcengine-plan//sk-only\n"),
    ):
        assert source_problem(text, name) is None


def test_sk_key_shape_requires_a_left_boundary_and_still_blocks_non_python_literals():
    body = "A1b2" * 8
    assert source_problem(f"label = 'prefixsk-{body}'\n", "app.py") is None
    assert source_problem(f'{{"value": "sk-{body}"}}', "model.json") == {
        "line": 1,
        "rule": "credential_pattern",
    }


@pytest.mark.parametrize("prefix", ["/", "=", "'", " ", "标签", "\u200b"])
def test_source_sk_key_shape_accepts_non_identifier_left_delimiters(prefix):
    body = "A1b2" * 8
    assert source_problem(f"value = '{prefix}sk-{body}'\n", "app.py")


def test_log_redaction_remains_conservative():
    assert "private-literal-value" not in redact("password=private-literal-value")
    assert "[REDACTED]" in redact("api_key=settings.api_key")
    assert redact_known("value='sk-fallback-key'") == "value='[REDACTED]'"
