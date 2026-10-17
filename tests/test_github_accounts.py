import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from repowatch.app import create_app
from repowatch.github_accounts import GitHubAccounts, GitHubClient, GitHubError
from repowatch.store import Conflict, Store

TOKEN_A = "test_token_alpha_not_a_real_credential"
TOKEN_B = "test_token_beta_not_a_real_credential"


class MemoryVault:
    def __init__(self):
        self.keys = {}
        self.unavailable = False

    def read_key(self, namespace):
        if self.unavailable:
            raise GitHubError("secure_storage_unavailable", 503)
        return self.keys.get(namespace)

    def write_key(self, namespace, key):
        if self.unavailable:
            raise GitHubError("secure_storage_unavailable", 503)
        self.keys[namespace] = key


class Remote:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.scopes = "repo, read:user"
        self.users = {
            TOKEN_A: {"id": 101, "login": "alice", "type": "User"},
            TOKEN_B: {"id": 202, "login": "bob", "type": "User"},
        }
        self.client = GitHubClient(httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "api.github.com"
        assert request.url.scheme == "https"
        assert request.method == "GET"
        if self.status != 200:
            return httpx.Response(self.status, json={"message": TOKEN_A},
                                  headers={"Location": "https://untrusted.example/"})
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token not in self.users:
            return httpx.Response(401, json={"message": token})
        if request.url.path == "/user":
            headers = {"X-OAuth-Scopes": self.scopes} if self.scopes is not None else {}
            return httpx.Response(200, json=self.users[token], headers=headers)
        assert request.url.path == "/repos/alice/project"
        return httpx.Response(200, json={
            "full_name": "alice/project", "private": True,
            "permissions": {"admin": True, "push": True},
        })


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "accounts.db")
    vault, remote = MemoryVault(), Remote()
    return store, vault, remote, GitHubAccounts(store, remote.client, vault)


def test_connect_encrypts_tokens_and_restores_identity_without_plaintext(setup, caplog):
    store, vault, remote, service = setup
    result = service.connect("Alice", TOKEN_A)
    assert result["active_account_id"] == 101
    assert result["accounts"][0]["login"] == "alice"
    assert result["accounts"][0]["scopes"] == ["read:user", "repo"]
    assert not result["github_publication"]
    assert TOKEN_A not in json.dumps(result)
    assert "encrypted_token" not in json.dumps(result)
    assert TOKEN_A not in caplog.text
    with store.connection() as db:
        dump = "\n".join(db.iterdump())
        assert TOKEN_A not in dump
        assert not any(key in dump for key in vault.keys.values())
    assert TOKEN_A.encode() not in store.path.read_bytes()
    restarted = GitHubAccounts(Store(store.path), remote.client, vault)
    restarted.verify(101, result["accounts"][0]["version"])
    assert restarted.list()["accounts"][0]["state"] == "verified"


def test_multiple_accounts_switch_rotation_and_disconnect(setup):
    _, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    service.connect("bob", TOKEN_B)
    assert service.list()["active_account_id"] == 202
    switched = service.verify(101, 1, activate=True)
    assert switched["active_account_id"] == 101
    assert len(switched["accounts"]) == 2
    rotated = service.connect("alice", TOKEN_A)
    assert len(rotated["accounts"]) == 2
    assert rotated["accounts"][0]["version"] == 3
    with pytest.raises(Conflict):
        service.disconnect(101, 1)
    result = service.disconnect(101, 3)
    assert result["active_account_id"] is None
    assert [a["login"] for a in result["accounts"]] == ["bob"]
    assert len(remote.calls) == 4


def test_identity_mismatch_cannot_change_saved_connection(setup):
    _, vault, _, service = setup
    before = service.connect("alice", TOKEN_A)
    keys = dict(vault.keys)
    with pytest.raises(GitHubError, match="github_account_mismatch"):
        service.connect("alice", TOKEN_B)
    assert service.list() == before
    assert vault.keys == keys


def test_invalid_token_does_not_switch_account(setup):
    _, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    service.connect("bob", TOKEN_B)
    remote.users.pop(TOKEN_A)
    with pytest.raises(GitHubError, match="github_invalid_token"):
        service.verify(101, 1, activate=True)
    result = service.list()
    assert result["active_account_id"] == 202
    assert result["accounts"][0]["state"] == "invalid"
    assert result["accounts"][0]["version"] == 2


def test_missing_master_key_and_unavailable_store_fail_closed(setup):
    _, vault, _, service = setup
    vault.unavailable = True
    with pytest.raises(GitHubError, match="secure_storage_unavailable"):
        service.connect("alice", TOKEN_A)
    assert service.list()["accounts"] == []
    vault.unavailable = False
    service.connect("alice", TOKEN_A)
    vault.keys.clear()
    assert service.list()["accounts"][0]["state"] == "credential_missing"
    with pytest.raises(GitHubError, match="github_credential_missing"):
        service.verify(101, 1)
    service.connect("alice", TOKEN_A)
    assert service.list()["accounts"][0]["state"] == "verified"
    vault.unavailable = True
    result = service.disconnect(101, 3)
    assert result["accounts"] == [] and result["active_account_id"] is None


def test_separate_databases_have_separate_vault_namespaces(setup, tmp_path):
    store, vault, remote, service = setup
    service.connect("alice", TOKEN_A)
    other = Store(tmp_path / "other.db")
    other_service = GitHubAccounts(other, remote.client, vault)
    other_service.connect("bob", TOKEN_B)
    assert len(vault.keys) == 2
    with store.connection() as db:
        encrypted = db.execute("SELECT encrypted_token FROM github_accounts").fetchone()[0]
    with other.connection() as db:
        db.execute("UPDATE github_accounts SET encrypted_token=?", (encrypted,))
    assert other_service.list()["accounts"][0]["state"] == "credential_missing"
    assert service.list()["accounts"][0]["state"] == "verified"


def test_credential_rotation_rolls_back_when_metadata_write_fails(setup):
    store, _, remote, service = setup
    before = service.connect("alice", TOKEN_A)
    rotated_token = "test_token_rotated_not_a_real_credential"
    remote.users[rotated_token] = remote.users[TOKEN_A]
    with store.connection() as db:
        db.execute("""
            CREATE TRIGGER reject_rotation BEFORE UPDATE ON github_accounts
            BEGIN SELECT RAISE(ABORT, 'test failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        service.connect("alice", rotated_token)
    assert service.list() == before
    with store.connection() as db:
        row = service.row(db, 101)
        assert service.token(service.cipher(db), row) == TOKEN_A


@pytest.mark.parametrize("status,code", [
    (401, "github_invalid_token"), (403, "github_forbidden"),
    (429, "github_rate_limited"), (500, "github_unavailable"),
    (302, "github_unavailable"),
])
def test_upstream_failures_are_redacted_and_redirects_not_followed(setup, status, code):
    _, _, remote, service = setup
    remote.status = status
    with pytest.raises(GitHubError, match=code) as error:
        service.connect("alice", TOKEN_A)
    assert TOKEN_A not in str(error.value)
    assert len(remote.calls) == 1
    assert service.list()["accounts"] == []


def test_missing_scope_header_does_not_invent_token_permissions(setup):
    _, _, remote, service = setup
    remote.scopes = None
    assert service.connect("alice", TOKEN_A)["accounts"][0]["scopes"] is None


def test_username_rename_preserves_identity_but_other_user_id_is_rejected(setup):
    _, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    remote.users[TOKEN_A]["login"] = "renamed"
    assert service.verify(101, 1)["accounts"][0]["login"] == "renamed"
    remote.users[TOKEN_A]["id"] = 999
    with pytest.raises(GitHubError, match="github_account_mismatch"):
        service.verify(101, 2)


def test_repository_check_is_read_only_and_does_not_claim_write_access(setup):
    store, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    repo = store.add_repository("alice/project", "https://github.com/alice/project")
    result = service.check_repository(101, 1, repo["id"])
    assert result["visible"] is True and result["private"] is True
    assert result["write_access"] == "not_verified"
    assert "permissions" not in result
    assert remote.calls[-1].url.path == "/repos/alice/project"
    before = len(remote.calls)
    with pytest.raises(GitHubError, match="github_repository_required"):
        service.check_repository(101, 1, "repo_fixture")
    assert len(remote.calls) == before
    with store.connection() as db:
        db.execute("UPDATE repositories SET url='https://evil.example' WHERE id=?", (repo["id"],))
    with pytest.raises(GitHubError, match="github_repository_required"):
        service.check_repository(101, 1, repo["id"])
    assert len(remote.calls) == before


def test_api_auth_csrf_redaction_and_lifecycle(setup):
    store, vault, remote, _ = setup
    app = create_app(store.path, start_worker=False,
                     github_client=remote.client, credential_vault=vault)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/github/accounts").status_code == 401
        auth = client.post("/api/setup", json={"password": "test-local-password"}).json()
        headers = {"X-CSRF-Token": auth["csrf"]}
        body = {"username": "alice", "token": TOKEN_A}
        assert client.post("/api/github/accounts", json=body).status_code == 403
        assert client.post("/api/github/accounts", json=body, headers={
            **headers, "Origin": "https://attacker.example",
        }).status_code == 403
        for invalid in (
            {**body, "scopes": ["repo"]}, {**body, "token": TOKEN_A * 20},
            {**body, "token": {"secret": TOKEN_A}}, {**body, "username": TOKEN_A + "!"},
        ):
            response = client.post("/api/github/accounts", json=invalid, headers=headers)
            assert response.status_code == 422
            assert TOKEN_A not in response.text
        connected = client.post("/api/github/accounts", json=body, headers=headers)
        assert connected.status_code == 200
        assert TOKEN_A not in connected.text and "encrypted_token" not in connected.text
        assert connected.headers["cache-control"] == "no-store"
        assert client.get("/api/system").json()["github_account_binding"]
        assert not client.get("/api/system").json()["github_publication"]
        assert client.post("/api/github/accounts/101/verify",
                           headers=headers, json={"version": 1}).status_code == 200
        assert client.post("/api/github/accounts/101/activate",
                           headers=headers, json={"version": 1}).status_code == 409
        assert client.post("/api/github/accounts/101/disconnect",
                           headers=headers, json={"version": 2}).json()["accounts"] == []
        assert client.post("/api/github/accounts/101/activate",
                           headers=headers, json={"version": 2}).status_code == 404
        assert client.post(f"/api/github/accounts/{2**64}/activate",
                           headers=headers, json={"version": 1}).status_code == 404
        assert client.post("/api/github/accounts/101/activate",
                           headers=headers, json={"version": 2**64}).status_code == 422


@pytest.mark.parametrize("payload", [
    {"id": True, "login": "alice", "type": "User"},
    {"id": 2**64, "login": "alice", "type": "User"},
    {"id": 101, "login": "alice", "type": "Organization"},
    {"id": 101, "login": "https://evil.example", "type": "User"},
    [],
])
def test_invalid_upstream_identity_is_never_saved(setup, payload):
    store, vault, _, _ = setup
    client = GitHubClient(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    service = GitHubAccounts(store, client, vault)
    with pytest.raises(GitHubError):
        service.connect("alice", TOKEN_A)
    assert not service.list()["accounts"] and not vault.keys


def test_network_exception_and_non_json_are_sanitized(setup):
    store, vault, _, _ = setup

    def timeout(request):
        raise httpx.ReadTimeout(TOKEN_A)

    client = GitHubClient(httpx.MockTransport(timeout))
    with pytest.raises(GitHubError, match="github_unreachable") as error:
        GitHubAccounts(store, client, vault).connect("alice", TOKEN_A)
    assert TOKEN_A not in str(error.value)
    client = GitHubClient(httpx.MockTransport(
        lambda request: httpx.Response(200, text=TOKEN_A),
    ))
    with pytest.raises(GitHubError, match="github_invalid_response"):
        GitHubAccounts(store, client, vault).connect("alice", TOKEN_A)


def test_repository_check_marks_revoked_credentials_invalid(setup):
    store, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    repo = store.add_repository("alice/project", "https://github.com/alice/project")
    remote.users.pop(TOKEN_A)
    with pytest.raises(GitHubError, match="github_invalid_token"):
        service.check_repository(101, 1, repo["id"])
    assert service.list()["accounts"][0]["state"] == "invalid"


def test_concurrent_credential_rotation_cannot_be_overwritten_by_old_verification(setup):
    store, _, remote, service = setup
    service.connect("alice", TOKEN_A)
    service.connect("bob", TOKEN_B)
    original = remote.client.identity

    def changed_while_verifying(token):
        with store.connection() as db:
            db.execute("UPDATE github_accounts SET version=version+1 WHERE id=101")
        return original(token)

    remote.client.identity = changed_while_verifying
    with pytest.raises(Conflict):
        service.verify(101, 1, activate=True)
    assert service.list()["active_account_id"] == 202
