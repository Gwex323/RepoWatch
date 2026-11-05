import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from repowatch.adapters.credentials import WindowsCredentialVault
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.credentials import CredentialError
from repowatch.application.providers import ProviderSaveError, ProviderService
from repowatch.application.storage import ConflictError
from repowatch.configuration import StorageSettings
from repowatch.domain.providers import ProviderInputError, parse_providers
from repowatch.operations.migrate import migrate


class FixtureVault:
    def __init__(self):
        self.values, self.calls, self.fail_at = {}, 0, None
        self.observe = lambda: None

    def create(self, reference, secret):
        self.calls += 1
        self.observe()
        if self.calls == self.fail_at or reference in self.values:
            raise CredentialError()
        self.values[reference] = secret

    def read(self, reference):
        if reference not in self.values:
            raise CredentialError()
        return self.values[reference]

    def delete(self, reference):
        self.values.pop(reference, None)


def provider(name="gateway", key="test-provider-key"):
    return {
        "name": name,
        "base_url": "https://gateway.example/v1",
        "api": "chat_completions",
        "api_key": key,
        "models": [{"model": "manual-model", "reasoning_effort": "low"}],
    }


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("保存或导入不允许网络调用")

    monkeypatch.setattr("httpx.Client.send", forbidden)
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    vault = FixtureVault()
    yield settings, store, vault, ProviderService(store, vault)
    store.close()


@pytest.mark.parametrize("shape", ["single", "array", "wrapped"])
def test_three_json_shapes_atomically_activate_without_network_or_secret_echo(kernel, shape):
    _, store, vault, service = kernel
    data = provider()
    payload = data if shape == "single" else [data] if shape == "array" else {"providers": [data]}
    snapshots = []
    vault.observe = lambda: snapshots.append(service.list())
    result = service.save("save", json.dumps(payload))
    assert result["state"] == "active"
    assert len(service.list()) == 1
    assert snapshots == [[]]
    assert "test-provider-key" not in json.dumps(result)
    assert "api_key" not in json.dumps(service.list())
    assert service.save("save", json.dumps(payload)) == result
    assert vault.calls == 1
    with pytest.raises(ConflictError):
        service.save("save", provider(key="test-provider-key-different"))
    with store.read() as records:
        assert len(records.find("provider_batches")) == 1
        assert records.find("provider_name_claims") == []
        assert "test-provider-key" not in json.dumps(
            [dict(row) for row in records.find("business_events")]
        )


def test_name_conflicts_batch_duplicates_and_invalid_second_item_write_nothing(kernel):
    _, _, vault, service = kernel
    original = service.save("first", provider())
    initial_values = dict(vault.values)
    with pytest.raises(ConflictError):
        service.save("conflict", [provider("new"), provider("GATEWAY")])
    for data in (
        [provider("a"), provider("A")],
        [provider("a"), {**provider("b"), "base_url": "https://name:secret@example/v1"}],
        [provider("a"), {**provider("b"), "unknown": "field"}],
        [provider("a"), {**provider("b"), "api_key": ""}],
    ):
        with pytest.raises(ProviderInputError):
            service.save(str(uuid4()), data)
    assert dict(vault.values) == initial_values
    assert service.list() == original["providers"]


def test_second_credential_failure_cleans_entire_batch_and_preserves_existing_keys(kernel):
    _, store, vault, service = kernel
    original = service.save("old", provider())
    initial_values = dict(vault.values)
    vault.fail_at = vault.calls + 2
    with pytest.raises(ProviderSaveError):
        service.save("batch", [provider("a"), provider("b")])
    assert service.list() == original["providers"]
    assert vault.values == initial_values
    with store.read() as records:
        assert records.find("provider_name_claims") == []
        assert all(
            row["state"] in {"deleted", "active"} for row in records.find("credential_stages")
        )
    vault.fail_at = None
    assert service.save("retry-new-operation", [provider("a"), provider("b")])["state"] == "active"
    assert len(service.list()) == 3


def test_edit_revisions_keep_old_credential_and_require_expected_version(kernel):
    _, _, vault, service = kernel
    original = service.save("old", provider())["providers"][0]
    old = service.revision(original["revision_id"])
    config = provider()
    config.pop("api_key")
    config["base_url"] = "http://127.0.0.1:9001/v1"
    edited = service.save(
        "edit",
        config,
        provider_id=original["id"],
        expected_revision=original["revision"],
    )["providers"][0]
    assert edited["revision"] == 2
    assert edited["credential_revision"] == 1
    assert vault.calls == 1
    rotated = service.save(
        "rotate",
        provider(key="test-rotated-key"),
        provider_id=original["id"],
        expected_revision=2,
    )["providers"][0]
    new = service.revision(rotated["revision_id"])
    assert vault.read(old["credential_reference"]).reveal() == "test-provider-key"
    assert vault.read(new["credential_reference"]).reveal() == "test-rotated-key"
    assert old["configuration_json"] != new["configuration_json"] or old["id"] != new["id"]
    with pytest.raises(ConflictError):
        service.save("stale", config, provider_id=original["id"], expected_revision=1)


def test_cross_storage_activation_failure_does_not_expose_partial_batch(kernel):
    _, store, vault, service = kernel
    original = service.save("old", provider())
    values = dict(vault.values)
    injected = []

    def fail_once(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE providers SET") and not injected:
            injected.append(True)
            raise OperationalError(statement, {}, sqlite3.OperationalError("injected full"))

    event.listen(store.engine, "before_cursor_execute", fail_once)
    try:
        with pytest.raises(ProviderSaveError):
            service.save("batch", [provider("a"), provider("b")])
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_once)
    assert service.list() == original["providers"]
    assert vault.values == values
    assert service.save("batch", [provider("a"), provider("b")])["state"] == "aborted"


def test_invalid_json_error_does_not_echo_input_and_rejects_duplicate_keys():
    for payload in ('{"api_key":"test-secret",', '{"name":"a","name":"b"}', "[]", "null"):
        with pytest.raises(ProviderInputError) as error:
            parse_providers(payload)
        assert "test-secret" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows 凭据跨进程退出验收")
def test_actual_crash_after_credential_staging_is_cleaned_without_touching_active_key(kernel):
    settings, store, _, _ = kernel
    namespace = "test-" + uuid4().hex
    vault = WindowsCredentialVault(namespace)
    service = ProviderService(store, vault)
    old = service.save("old-real", provider())["providers"][0]
    old_reference = service.revision(old["revision_id"])["credential_reference"]
    script = """
import os, sys
from pathlib import Path
from repowatch.configuration import StorageSettings
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.adapters.credentials import WindowsCredentialVault
from repowatch.application.providers import ProviderService
class CrashVault:
    def __init__(self): self.actual = WindowsCredentialVault(sys.argv[2])
    def create(self, reference, secret):
        self.actual.create(reference, secret)
        os._exit(17)
store = SQLiteBusinessStore(StorageSettings(Path(sys.argv[1])))
ProviderService(store, CrashVault()).save('crash', {
    'name': 'staged', 'base_url': 'https://unused.example/v1',
    'api': 'responses', 'api_key': 'test-crash-credential', 'models': [],
})
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script, str(settings.data_dir), namespace],
            capture_output=True,
            timeout=15,
        )
        assert completed.returncode == 17
        assert len(service.list()) == 1
        assert service.reconcile()[0]["state"] == "cleaned"
        assert vault.read(old_reference).reveal() == "test-provider-key"
        assert len(service.list()) == 1
        with store.read() as records:
            references = records.find("credential_stages")
            crashed = [row for row in references if row["reference"] != old_reference]
            assert all(row["state"] == "deleted" for row in crashed)
        for row in crashed:
            with pytest.raises(CredentialError):
                vault.read(row["reference"])
    finally:
        with store.read() as records:
            for row in records.find("credential_stages"):
                vault.delete(row["reference"])
