# ADR 0012: Shared Repository Memory

Date: 2026-10-10
Status: implemented for the controlled-fixture workbench

Amended by ADR 0013: reproduction authorization is now origin-dependent.
Existing-Issue samples record automatic workflow evidence, not owner
confirmation. Both origins still require a verified patch before fix memory.
The shared-memory isolation and reference-only rules remain unchanged.

## Decision

Use one SQLite-backed memory service shared by the six Agents SDK roles.
Every entry belongs to one registered repository and one exact revision.
The backend derives both identifiers from the current run, not from model
arguments. There is no cross-repository or global memory lookup for agents.

Keep three separate responsibilities:

- Business records and SDK checkpoints remain the authoritative task state.
- Memory entries are optional reference material, never authorization.
- Memory revisions and per-role reads retain an audit of what was supplied.

The existing Agent graph, tools, confirmation gates, and snapshot format are
unchanged. Dynamic instructions supply a bounded repository-reference JSON
section before each model call. This is not an Agents SDK conversation Session
and does not copy all previous transcripts into a new run.

## Stored Knowledge

Owner-maintained notes cover architecture, setup, and testing. They are
explicitly unverified, even when the owner writes that something is approved.
The owner must specify the exact applicable revision. Empty or oversized
fields are rejected.

Workflow-generated records cover:

- Human-confirmed reproduction, with the source run and observation.
- Negative reproduction decisions, without claiming the bug is absent.
- Fixture repair experience, only after human confirmation and passing
  fixture verification with a patch artifact present.

These records are generated from application evidence using fixed templates,
not model reflection. The current fixture patch is predefined; its verification
does not establish a tested real-repository patch or a merged PR. Automatic
knowledge extraction for downloaded repositories is not implemented.

Human decision and memory creation share the confirmation/enqueue transaction.
Fix memory and task completion share a transaction. Source-event keys prevent
duplicates on retries; separate runs keep separate evidence records. This is
not cross-run finding deduplication.

## Retrieval

Every role receives only active entries from its run's repository and exact
revision, round-robin across role-prioritized categories, newest within each
category. This keeps repeated bug records from starving setup/test knowledge. At most
six entries and 6,000 JSON characters are supplied per invocation. This is a
character budget, not an exact token budget. Entries that do not fit are
omitted, not silently truncated. Evidence remains accessible in the WebUI
through the source task and memory record.

Role priorities:

| Role | Categories, in priority order |
| --- | --- |
| Repository analyst | architecture, setup, testing, bug |
| Problem investigator | bug, investigation, testing, architecture |
| Reproduction assistant | setup, testing, bug, investigation |
| Repair agent | fix, bug, architecture, testing |
| Independent reviewer | testing, bug, fix, investigation |
| PR author | bug, fix, testing |

Selection is refreshed on model calls, including after a checkpoint resume.
Archive and edit operations affect subsequent selections; already supplied
material cannot be retroactively unseen. The audit retains its original
version, even after the entry is edited or archived. A read audit means
"supplied as context", not proof that the model relied on it.

The JSON is explicitly labeled untrusted reference data. This instruction is
defense in depth, not the security boundary: sensitive tools still check the
current run's own confirmation and revision. Memory grants no tool approvals,
publication authority, or merge capability.

## WebUI And API

The repository memory view provides repository selection, literal substring
search, category/revision/status filters, pagination, note creation/editing,
history, evidence, archive, restore, and links to source runs. Finding details
include a per-role memory-reference tab. Workflow evidence is immutable;
owners can archive or restore it but cannot rewrite it as verified knowledge.

Writes require the existing owner login, same-origin and CSRF controls.
Updates, archive, and restore require the version displayed by the client;
stale writes fail rather than overwriting concurrent changes. Queries involving
entry IDs always include the repository ID.

## Migration And Retention

Schema changes are additive. On the first upgraded startup, existing
current-revision controlled-fixture confirmations and completed verified
results are imported once. No historical SDK snapshot is modified. Imported
entries that are later archived are not recreated on restart.

Old-version entries remain visible as history but are not supplied to
different-revision tasks. There is no automatic TTL, hard deletion, vector
index, embedding service, semantic search, cross-version generalization,
or LLM summarization in this increment. Archive removes an entry from future
selection; it does not delete evidence or audit records.

The database contains private project notes, evidence and login state.
Do not paste credentials into notes. Protect and back up the entire database;
memory and audit records migrate with the existing task database.

## Verification

Backend tests cover repository/revision isolation, bounded role retrieval,
literal search, immutable evidence, optimistic writes, reference-version
audits, transactional rollback, restart/resume, old checkpoints, schema
migration and one-time backfill, authentication, CSRF, and forged metadata.
Browser tests cover memory CRUD, repository switching, version filtering,
six-role references, archive/restore, and mobile overflow.

All model calls remain deterministic fixtures. Tests verify context delivery
and application gates, not the quality of real-model reasoning or resistance
of a live model to every prompt-injection attempt.
