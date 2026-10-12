# ADR 0014: GitHub Account Binding

Date: 2026-10-12
Status: implemented for identity and read-only repository access
Branch: codex/github-account-binding

## Scope

Add account management to the single-owner WebUI. The owner can connect personal
GitHub accounts using a PAT, select one active account, revalidate, replace its
token, inspect access to a registered repository, and disconnect. This increment
does not implement OAuth/device authorization, import the host's `gh` token,
execute repositories, or add Issue, PR, push, fork, merge, or auto-merge tools.

Each connection checks `GET /user` against the fixed `api.github.com` host.
The expected username must match the API response, case-insensitively. Save the
GitHub numeric user ID as the durable identity; a later username change keeps
that identity. A changed numeric ID fails validation.

Connecting selects the verified account. Switching revalidates credentials
before changing the active selection. Invalid authorization cannot switch it.
Disconnecting the active account leaves no active identity, even when another
saved account exists. Disconnecting removes the local connection; it does not
revoke the PAT in GitHub.

## Storage And Recovery

The additive `github_accounts` table stores identity, last verified scope header,
state, timestamps, an optimistic version, and Fernet-encrypted token bytes.
`github_configuration` stores a random per-installation credential namespace
and an active-account foreign key with `ON DELETE SET NULL`.

The encryption key is stored separately through keyring in an explicitly chosen
OS backend: Windows Credential Manager, macOS Keychain, or Linux Secret Service.
The application never selects a plaintext or null keyring fallback. Key creation
and token updates are serialized with SQLite's immediate transaction. Rotation,
account metadata, and active selection commit together. Deletion removes the
encrypted token in the same transaction as the binding. The installation key
remains for other connections and later reuse.

If the OS key disappears or cannot decrypt an old record, the API reports
`credential_missing`; reconnecting can establish a new credential. Copying a
database to another machine does not carry the OS key, so reconnect there.
Headless Linux needs an available, unlocked Secret Service before binding;
there is no automatic file-key fallback. Linux support is designed but not
verified on a Linux host in this delivery.

Encrypted historical bytes may remain in SQLite WAL files or older backups.
Local disconnect is not a promise of secure physical erasure or remote token
revocation. Database and key-store protection depend on the host user account.

## Request And Error Boundaries

All account routes require the existing owner session, same-origin checks, and
CSRF protection on writes. Secrets are only accepted in a POST body. Pydantic
uses `SecretStr`, and validation errors on these routes do not echo rejected
input. Public responses explicitly select metadata and never include ciphertext,
tokens, or the vault namespace/key. Form token state is cleared on submission.

The GitHub HTTP adapter has a fixed HTTPS origin, a ten-second timeout,
`follow_redirects=False`, and `trust_env=False`. No caller can supply a base URL,
credential header, or arbitrary remote endpoint. Proxy environment settings are
not used by this adapter. Network errors and non-success responses become fixed
error codes; upstream bodies and exception details are not reflected or logged.

Revalidation occurs outside the database write transaction. Successful and
failed results are applied only to the credential version that was checked,
preventing an older check from overwriting a newer token. Transient errors or
rate limits do not label the token revoked. Only rejected identity/authorization
or unavailable credential material marks it invalid or missing.

## Permission Meaning

The authenticated-user response establishes identity, not repository privileges.
Traditional OAuth scope headers are shown when present; a missing header stays
unknown instead of implying unrestricted access.

The repository access action accepts only a registered GitHub repository ID.
It verifies the token's user identity and calls `GET /repos/{owner}/{repo}` at
the fixed host, rejecting redirects and mismatched repository responses.
Success means repository metadata is readable with this credential. It does
not establish clone/content access, Issue creation, PR creation, or branch push.
Even repository metadata `push`/`admin` flags do not prove the token's complete
fine-grained permission set, so the response says write access is not verified.

When real remote jobs are added, they must pin the selected account ID and
revalidate operation-specific permission. This increment deliberately does not
make existing fixture jobs use the active account or give agents its secret.

## Verification

Backend tests use a mock HTTP transport and an in-memory vault to cover
identity mismatch, rename, multiple connections, activation, rotation, rollback,
disconnect, stale updates, key loss, database isolation, error redaction,
authentication/CSRF, expired tokens, redirects, malformed responses, and
read-only repository checks. These tests do not consume personal credentials.

Browser tests cover connection mismatch, token-field clearing, multiple-account
selection, scope uncertainty, expired-credential recovery, repository checks,
disconnect, and desktop/mobile layout with synthetic GitHub responses.
Windows OS-vault availability and write/read/delete were separately probed using
a temporary non-secret test value that was removed afterward.
