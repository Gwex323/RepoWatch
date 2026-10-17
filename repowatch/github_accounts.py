"""Authenticated GitHub identity and encrypted credentials; no publication tools."""

import json
import re
import sys
import time

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .store import Conflict, Missing, uid

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
SCOPES = {
    "repo", "public_repo", "read:user", "user", "user:email", "user:follow",
    "read:org", "write:org", "admin:org", "workflow", "gist", "delete_repo",
    "admin:repo_hook", "write:repo_hook", "read:repo_hook", "repo:status",
    "repo_deployment", "repo:invite", "security_events", "project", "read:project",
}


class GitHubError(Exception):
    def __init__(self, code, status=400, *, details=None):
        self.code = code
        self.status = status
        self.details = details or {}
        super().__init__(code)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS github_accounts (
            id INTEGER PRIMARY KEY,
            login TEXT NOT NULL,
            encrypted_token TEXT NOT NULL,
            scopes TEXT,
            state TEXT NOT NULL,
            checked_at REAL NOT NULL,
            updated REAL NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS github_configuration (
            id INTEGER PRIMARY KEY CHECK(id=1),
            namespace TEXT NOT NULL,
            active_account_id INTEGER REFERENCES github_accounts(id) ON DELETE SET NULL
        );
    """)
    db.execute(
        "INSERT OR IGNORE INTO github_configuration(id,namespace) VALUES(1,?)",
        (uid("vault"),),
    )


class SystemVault:
    """Use an explicit OS credential backend; never fall back to plaintext."""

    @staticmethod
    def backend():
        try:
            if sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring
                backend = WinVaultKeyring()
            elif sys.platform == "darwin":
                from keyring.backends.macOS import Keyring
                backend = Keyring()
            elif sys.platform.startswith("linux"):
                from keyring.backends.SecretService import Keyring
                backend = Keyring()
            else:
                raise RuntimeError("Unsupported platform")
            if backend.priority <= 0:
                raise RuntimeError("Secure store unavailable")
            return backend
        except Exception:
            raise GitHubError("secure_storage_unavailable", 503) from None

    def read_key(self, namespace):
        try:
            return self.backend().get_password("RepoWatch", namespace)
        except Exception:
            raise GitHubError("secure_storage_unavailable", 503) from None

    def write_key(self, namespace, key):
        try:
            self.backend().set_password("RepoWatch", namespace, key)
        except Exception:
            raise GitHubError("secure_storage_unavailable", 503) from None


class GitHubClient:
    def __init__(self, transport=None):
        self.transport = transport

    def request(self, path, token):
        result, headers = self.request_json("GET", path, token)
        if not isinstance(result, dict):
            raise GitHubError("github_invalid_response", 502)
        return result, headers

    def request_json(self, method, path, token, payload=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise GitHubError("github_invalid_input", 422)
        try:
            with httpx.Client(
                base_url="https://api.github.com", timeout=10, follow_redirects=False,
                trust_env=False, transport=self.transport,
            ) as client:
                response = client.request(method, path, json=payload, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "RepoWatch/0.1",
                })
        except httpx.HTTPError:
            raise GitHubError("github_unreachable", 502) from None
        if response.status_code == 401:
            raise GitHubError("github_invalid_token", 401)
        if response.status_code in (403, 429):
            raise self.access_error(response, method, path)
        if response.status_code == 404:
            raise GitHubError("github_repository_unavailable", 404)
        if response.status_code == 422:
            raise GitHubError("github_write_rejected", 422)
        if response.status_code not in (200, 201, 202, 204):
            raise GitHubError("github_unavailable", 502)
        if response.status_code == 204:
            return {}, response.headers
        try:
            result = response.json()
        except ValueError:
            raise GitHubError("github_invalid_response", 502) from None
        if not isinstance(result, dict):
            if isinstance(result, list):
                return result, response.headers
            raise GitHubError("github_invalid_response", 502)
        return result, response.headers

    @staticmethod
    def access_error(response, method, path):
        """Classify access failures; retain only bounded, allowlisted diagnostics."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message", "")).lower() if isinstance(body, dict) else ""
        remaining = response.headers.get("x-ratelimit-remaining", "")
        retry_after = response.headers.get("retry-after", "")
        limited = (
            response.status_code == 429 or remaining == "0" or retry_after.isdecimal()
            or any(text in message for text in (
                "api rate limit exceeded", "secondary rate limit", "abuse detection",
            ))
        )
        denied = any(text in message for text in (
            "resource not accessible by personal access token",
            "resource not accessible by integration", "insufficient permission",
        ))
        code = ("github_rate_limited" if limited else
                "github_permission_denied" if denied else "github_forbidden")
        details = {"status": response.status_code, "method": method.upper()}
        suffix = path.split("?", 1)[0]
        operations = {
            "/git/blobs": "git_blob", "/git/trees": "git_tree",
            "/git/commits": "git_commit", "/git/refs": "git_ref",
            "/issues": "issue", "/pulls": "pull_request", "/forks": "fork",
        }
        details["operation"] = next(
            (value for key, value in operations.items() if suffix.endswith(key)), "read_access"
        )
        for header, key in (
            ("x-ratelimit-remaining", "remaining"), ("retry-after", "retry_after"),
            ("x-ratelimit-reset", "reset_at"),
        ):
            value = response.headers.get(header, "")
            if re.fullmatch(r"[0-9]{1,12}", value):
                details[key] = int(value)
        accepted = response.headers.get("x-accepted-github-permissions", "")
        alternatives = []
        for group in accepted[:512].split(";"):
            permissions = {}
            for item in group.split(","):
                match = re.fullmatch(
                    r"\s*(contents|issues|pull_requests|administration|metadata)=(read|write)\s*",
                    item,
                )
                if match:
                    key, value = match.groups()
                    if permissions.get(key) != "write":
                        permissions[key] = value
            if permissions and permissions not in alternatives:
                alternatives.append(permissions)
        if len(alternatives) == 1:
            details["required_permissions"] = alternatives[0]
        elif alternatives:
            details["permission_alternatives"] = alternatives
        request_id = response.headers.get("x-github-request-id", "")
        if re.fullmatch(r"[A-Za-z0-9:-]{1,100}", request_id):
            details["request_id"] = request_id
        return GitHubError(code, 429 if limited else 403, details=details)

    def identity(self, token):
        data, headers = self.request("/user", token)
        if (
            type(data.get("id")) is not int or not 1 <= data["id"] < 2**63
            or not isinstance(data.get("login"), str)
            or not LOGIN.fullmatch(data["login"]) or data.get("type") != "User"
        ):
            raise GitHubError("github_invalid_identity", 502)
        scopes = headers.get("x-oauth-scopes")
        return {
            "id": data["id"], "login": data["login"],
            "scopes": sorted(set(scopes.replace(" ", "").split(",")) & SCOPES)
            if scopes is not None else None,
        }

    def repository(self, token, name):
        data, _ = self.request(f"/repos/{name}", token)
        if (
            not isinstance(data.get("full_name"), str)
            or data["full_name"].casefold() != name.casefold()
            or type(data.get("private")) is not bool
        ):
            raise GitHubError("github_invalid_response", 502)
        return {
            "repository": data["full_name"], "visible": True, "private": data["private"],
            "write_access": "not_verified",
        }


class GitHubAccounts:
    def __init__(self, store, client=None, vault=None):
        self.store = store
        self.client = client or GitHubClient()
        self.vault = vault or SystemVault()

    def cipher(self, db, *, create=False):
        namespace = db.execute(
            "SELECT namespace FROM github_configuration WHERE id=1"
        ).fetchone()["namespace"]
        key = self.vault.read_key(namespace)
        if key is None and create:
            key = Fernet.generate_key().decode("ascii")
            self.vault.write_key(namespace, key)
        if key is None:
            return None
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise GitHubError("secure_storage_unavailable", 503) from None

    @staticmethod
    def row(db, account_id, version=None):
        if type(account_id) is not int or not 1 <= account_id < 2**63:
            raise Missing("GitHub account not found.")
        row = db.execute("SELECT * FROM github_accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise Missing("GitHub account not found.")
        if version is not None and row["version"] != version:
            raise Conflict("GitHub account changed. Refresh and try again.")
        return row

    @staticmethod
    def token(cipher, row):
        try:
            if cipher is None:
                raise InvalidToken
            return cipher.decrypt(row["encrypted_token"].encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError, UnicodeError):
            raise GitHubError("github_credential_missing", 409) from None

    def list(self):
        with self.store.connection() as db:
            configuration = db.execute("SELECT * FROM github_configuration").fetchone()
            try:
                cipher = self.cipher(db)
                storage = "available"
            except GitHubError:
                cipher, storage = None, "unavailable"
            accounts = []
            for row in db.execute("SELECT * FROM github_accounts ORDER BY login COLLATE NOCASE"):
                state = row["state"]
                try:
                    self.token(cipher, row)
                except GitHubError:
                    state = "credential_missing"
                accounts.append({
                    "id": row["id"], "login": row["login"], "state": state,
                    "scopes": json.loads(row["scopes"]) if row["scopes"] else None,
                    "checked_at": row["checked_at"], "version": row["version"],
                    "active": row["id"] == configuration["active_account_id"],
                })
        return {
            "accounts": accounts, "active_account_id": configuration["active_account_id"],
            "secure_storage": storage, "github_publication": False,
        }

    def connect(self, expected_login, token):
        if (
            not LOGIN.fullmatch(expected_login) or not 20 <= len(token) <= 512
            or not re.fullmatch(r"[A-Za-z0-9_]+", token)
        ):
            raise GitHubError("github_invalid_input", 422)
        identity = self.client.identity(token)
        if identity["login"].casefold() != expected_login.casefold():
            raise GitHubError("github_account_mismatch", 409)
        with self.store.connection() as db:
            # Serialize key creation and credential rotation across backend workers.
            db.execute("BEGIN IMMEDIATE")
            cipher = self.cipher(db, create=True)
            encrypted = cipher.encrypt(token.encode("ascii")).decode("ascii")
            now = time.time()
            db.execute(
                "INSERT INTO github_accounts "
                "(id,login,encrypted_token,scopes,state,checked_at,updated) "
                "VALUES(?,?,?,?,'verified',?,?) ON CONFLICT(id) DO UPDATE SET "
                "login=excluded.login,encrypted_token=excluded.encrypted_token,"
                "scopes=excluded.scopes,state='verified',checked_at=excluded.checked_at,"
                "updated=excluded.updated,version=github_accounts.version+1",
                (identity["id"], identity["login"], encrypted,
                 json.dumps(identity["scopes"]), now, now),
            )
            db.execute("UPDATE github_configuration SET active_account_id=? WHERE id=1",
                       (identity["id"],))
        return self.list()

    def validated_identity(self, account_id, version):
        with self.store.connection() as db:
            row = self.row(db, account_id, version)
            try:
                token = self.token(self.cipher(db), row)
            except GitHubError as exc:
                if exc.code != "github_credential_missing":
                    raise
                token = None
        try:
            if token is None:
                raise GitHubError("github_credential_missing", 409)
            identity = self.client.identity(token)
            if identity["id"] != account_id:
                raise GitHubError("github_account_mismatch", 409)
        except GitHubError as exc:
            if exc.code in ("github_invalid_token", "github_credential_missing",
                            "github_account_mismatch"):
                state = "credential_missing" if token is None else "invalid"
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE github_accounts SET state=?,version=version+1,updated=? "
                        "WHERE id=? AND version=?", (state, time.time(), account_id, version),
                    )
            raise
        return token, identity

    def verify(self, account_id, version, *, activate=False):
        _, identity = self.validated_identity(account_id, version)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.row(db, account_id, version)
            db.execute(
                "UPDATE github_accounts SET login=?,scopes=?,state='verified',checked_at=?,"
                "updated=?,version=version+1 WHERE id=?",
                (identity["login"], json.dumps(identity["scopes"]),
                 time.time(), time.time(), account_id),
            )
            if activate:
                db.execute("UPDATE github_configuration SET active_account_id=? WHERE id=1",
                           (account_id,))
        return self.list()

    def disconnect(self, account_id, version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.row(db, account_id, version)
            db.execute("DELETE FROM github_accounts WHERE id=?", (account_id,))
        return self.list()

    def check_repository(self, account_id, version, repo_id):
        with self.store.connection() as db:
            self.row(db, account_id, version)
            repo = db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
            if not repo or repo["kind"] != "github":
                raise GitHubError("github_repository_required", 422)
            match = re.fullmatch(
                r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100})",
                repo["url"],
            )
            if not match or match[1].split("/")[-1] in (".", ".."):
                raise GitHubError("github_repository_required", 422)
        token, _ = self.validated_identity(account_id, version)
        result = self.client.repository(token, match[1])
        with self.store.connection() as db:
            self.row(db, account_id, version)
        return {**result, "account_id": account_id, "checked_at": time.time()}
