import io
import json
import logging
import os
from uuid import uuid4

import pytest

from repowatch.adapters.credentials import WindowsCredentialVault
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import EventReader
from repowatch.application.credentials import CredentialError, SecretValue
from repowatch.application.task_records import business_event
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate
from repowatch.security import DEFAULT_REDACTOR, RedactingLogFilter, Redactor


def test_secret_repr_structured_config_and_known_encoded_values_are_redacted():
    secret = SecretValue("test-private+/key-value")
    redactor = Redactor()
    redactor.register(secret)
    payload = redactor.clean(
        {
            "api_key": secret.reveal(),
            "nested": {"authorization": "Bearer " + secret.reveal()},
            "message": "upstream said " + secret.reveal(),
            "secret_object": secret,
            "encoded": "test-private%2B%2Fkey-value",
        }
    )
    rendered = json.dumps(payload) + repr(secret) + str(secret)
    assert secret.reveal() not in rendered
    assert "test-private%2B%2Fkey-value" not in rendered
    assert "[REDACTED]" in rendered


def test_logging_redacts_arguments_exceptions_and_extra_fields():
    secret = SecretValue("unit-test-key-8675309")
    redactor = Redactor()
    redactor.register(secret)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RedactingLogFilter(redactor))
    handler.setFormatter(logging.Formatter("%(message)s %(details)s"))
    logger = logging.getLogger("repowatch.test.credentials")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        try:
            raise ValueError("SDK echoed " + secret.reveal())
        except ValueError:
            logger.exception(
                "model error: %s", secret.reveal(), extra={"details": {"api_key": secret}}
            )
        logger.info("Authorization: Bearer unknown-sensitive-value", extra={"details": {}})
    finally:
        logger.removeHandler(handler)
    assert secret.reveal() not in output.getvalue()
    assert "unknown-sensitive-value" not in output.getvalue()
    assert "Traceback" not in output.getvalue()


def test_business_events_persist_only_redacted_values(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    secret = SecretValue("test-event-key-12345")
    DEFAULT_REDACTOR.register(secret)
    try:
        with store.write() as records:
            business_event(
                records, "test", api_key=secret.reveal(), detail="error " + secret.reveal()
            )
        serialized = json.dumps(EventReader(store).read())
        assert secret.reveal() not in serialized
        with store.read() as records:
            assert secret.reveal() not in records.find("business_events")[0]["payload_json"]
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows 宿主凭据适配")
def test_actual_windows_vault_restart_revision_immutability_and_cleanup():
    namespace = "test-" + uuid4().hex
    secret = SecretValue("test-only-" + uuid4().hex)
    vault = WindowsCredentialVault(namespace, Redactor())
    try:
        vault.create("revision1", secret)
        reopened = WindowsCredentialVault(namespace, Redactor())
        assert reopened.read("revision1").reveal() == secret.reveal()
        with pytest.raises(CredentialError):
            reopened.create("revision1", SecretValue("another-test-key"))
        assert reopened.read("revision1").reveal() == secret.reveal()
        with pytest.raises(ValueError):
            reopened.read("../other-app")
        reopened.delete("revision1")
        with pytest.raises(CredentialError) as error:
            reopened.read("revision1")
        assert secret.reveal() not in str(error.value)
    finally:
        vault.delete("revision1")
