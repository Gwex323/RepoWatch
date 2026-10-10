# ADR 0013: Origin-Dependent Reproduction

Date: 2026-10-10
Status: implemented for controlled fixtures; real Issue intake remains planned

## Decision

System-discovered findings require personal reproduction before Issue creation
or repair. Existing GitHub Issues do not require personal reproduction, but do
require automatic validation before repair. Both paths require passing tests
and independent review before PR preparation. Project owners review and merge
externally; the system never merges or enables auto-merge.

This supersedes universal human-confirmation wording in earlier ADRs and the
architecture draft. It does not enable GitHub publication or real repository
execution in the current milestone.

## Trusted Origin

The backend assigns `source_kind` at task creation. Existing rows migrate to
`discovered`. A database trigger prevents changing either `source_kind` or
`issue_ref`; no API accepts either value as task authority.

The only existing-Issue intake currently implemented is a dedicated, authenticated
sample endpoint. It uses the fixed `fixture://page-window/issues/1` identity in
the built-in fixture repository. It cannot select an arbitrary repository.
Ordinary repository-run requests remain discoveries and reject extra body fields.

A future GitHub intake adapter must verify the Issue against GitHub, bind its
repository and identity, deduplicate against known findings, and preserve
discovery origin when publishing/linking its Issue. Publishing a discovery's
Issue is not a new existing-Issue intake. The current sample identity is not a
GitHub URL or proof of any remote access.

## Execution And Evidence

Keep the six native SDK roles and existing tool identities. The historical
`human_reproduction` tool uses conditional approval: discoveries interrupt and
save SDK state; existing-Issue samples execute automatic reproduction without
an approval interruption. The tool's implementation independently checks origin.
Existing discovered-finding snapshots retain the same graph and tool names.

Automatic evidence is separate from `confirmation` and `observation`; it records
the workflow actor, run ID, repository, Issue identity, revision, timestamp, and
actual reproduction result. A successful record cannot authorize another task,
another Issue, another revision, or a discovery. No fake owner record is created.
The reproduction evidence helper checks the appropriate current record at each
protected handoff/tool and before recording artifacts or completing a task.

Failed automatic validation becomes `validation_blocked`, saves failed evidence,
and appears in WebUI notifications. Retry uses the existing durable queue and
three-attempt limit; it reruns validation without changing origin. SDK-wrapped
validation failures are recognized by their typed exception cause, not message
text. Other execution failures remain ordinary failed tasks.

Existing-Issue samples skip Issue-draft creation and link their PR draft to the
sample Issue. Passing patch verification is required for PR drafts and task
completion. Retried reviews replace their earlier verification result. All
drafts remain local; publication and merge flags cannot be enabled.

## Memory And Migration

Automatic bug memory is generated in the same transaction as its evidence.
Human bug memory remains tied to the confirmation transaction. Source labels
distinguish automatic and human reproduction. Fix memory requires the applicable
current evidence and passing patch checks. Memory remains repository/revision
scoped reference data and never grants authorization.

Schema changes add three columns and an immutable-origin trigger. Existing
snapshots, fixture revision, confirmations, and memory history are not rewritten.
Migration is serialized across Store initializers. Pre-change unconditional
approval snapshots are explicitly covered by a migration-and-resume test.

## Verification

Tests cover both complete paths, gate bypass attempts, forged source fields,
immutable origin, evidence scope, automatic-validation pause/retry, failed review,
memory actor attribution, legacy migration/restoration, and no duplicate Issue
draft. Browser tests cover desktop/mobile automatic validation, missing human
controls on that path, three artifacts, and layout overflow.

These are deterministic fixture tests. They do not establish real Issue
ingestion, autonomous diagnosis, real patch execution, or GitHub integration.
