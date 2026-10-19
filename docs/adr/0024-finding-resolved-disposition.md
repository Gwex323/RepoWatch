# ADR 0024: Resolved disposition for historical findings

Date: 2026-10-19 (Asia/Shanghai)
Status: implemented

## Problem

ADR 0023 gave owners a way to close a historical finding as `not_reproduced`,
but confirmed findings had no positive end state. A genuinely fixed issue
stayed `confirmed` forever, kept entering the bounded historical recheck
candidate set, and consumed model calls each round even after its PR was
merged or the owner fixed it manually. Model `fixed` outcomes deliberately
cannot change status, so only an explicit owner action could close this gap.

## Decision

Add a `resolved` finding status. The disposition endpoint now accepts
`decision=not_reproduced|resolved`; both apply to findings in
`waiting_reproduction` or `confirmed` status and reuse the finding-version CAS,
owner session, and CSRF protections from ADR 0023.

Resolution evidence has two tiers:

- **Published fix**: when the finding has a `pr_created` repair task with a
  stored remote record, the backend verifies the recorded PR through the
  GitHub API with the account that published it. Only `merged: true` and a
  matching PR URL are accepted; the note is optional and the audit record
  stores `resolution: merged_pr` with the PR URL. An unmerged PR fails with
  `resolution_pr_not_merged`; account, network, or malformed-record failures
  fail with `resolution_verification_failed`. Verification runs outside the
  SQLite write transaction; the finding CAS closes the race window.
- **Owner assertion**: without a published fix, the owner may assert manual
  resolution. The note is mandatory and the audit record stores
  `resolution: owner_assertion`.

Repair interaction is unchanged from ADR 0023: queued repairs are cancelled in
the same transaction, running repairs block disposition, and terminal repair
records are preserved. A `resolved` finding leaves the historical candidate
set through the existing status filter; snapshots and stored reports are never
rewritten. Model `fixed` outcomes never trigger or auto-suggest this state.

## UI

Historical items in `waiting_reproduction` or `confirmed` live status now show
two owner actions: mark not reproduced (including false positives) and mark
resolved. The status map displays `已解决`; the current-status line appears
when the live status differs from the frozen origin status.

## Verification

Regression coverage includes owner-assertion note enforcement and candidate
exclusion, merged-PR acceptance with recorded PR evidence, unmerged-PR and
verification-failure rejection, queued-repair cancellation, running-repair
blocking, CAS/closed conflicts, and API owner/CSRF/forged-field enforcement.
Browser tests cover both disposition buttons, request payloads, the resolved
current-status line, button removal after disposition, and mobile overflow.
