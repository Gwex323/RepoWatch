# Implementation Plan

Date: 2026-10-09
Status updated: 2026-10-28

## Current Delivery Status

- The original execution, analysis, controlled repair and GitHub publication
  increments are implemented. Real GitHub Issues can be synchronized and used
  for analysis; daily repository checks and their history are available.
- The Windows main path has undergone real-model and real-GitHub-write acceptance.
  This is not universal provider or repository acceptance; independent live
  diagnosis and Linux migration acceptance remain outstanding.
- Central coordination (#77) is a separate six-stage effort. Unified fault
  recording (#78) was merged via PR #84 on 2026-10-28. Automatic recovery (#79),
  the coordinator (#80), self-maintenance (#81), deployment/service recovery
  (#82), and full acceptance (#83) remain open. A merged change is not proof
  that the running workbench has been deployed.
- Stage 1 validation recorded 1024 passing, 35 failing and 12 skipped backend
  tests. The same 35 failures appeared on its unmodified base; no new failures
  were identified in that comparison. That historical run was not green.
  See [the validation record](system-fault-records-78-validation.md).
- Issue #88 corrects stale repair/review test contracts and fixtures on
  `feature/main-regression-88`. The full backend run now reports 1061 passed,
  0 failed and 12 skipped; the skipped cases require explicit real Docker
  integration opt-in. Production behavior and recovery budgets are unchanged.
  This regression cleanup is implemented and verified on its branch, not merged
  or deployed. See [the regression validation](main-regression-88-validation.md).
- Automatic merging is not available; the project owner merges on GitHub.
  See the [README](../README.md) for current operational limits.

The dated increments and milestone sections below preserve the state and acceptance
scope at the time they were written. Their statements about what was not yet
implemented or verified do not override this current status or later ADRs.

## Milestone-One Delivery

Implemented: a local FastAPI/SQLite service, React/TypeScript WebUI, six Python
Agents SDK roles, native handoffs, an SDK approval interruption, authenticated
WebUI confirmation, saved-state resumption, and a durable leased task queue.

This delivery uses deterministic model responses and a trusted built-in
pagination fixture. Findings and repair drafts are predefined fixture content,
not outputs from live-model repository analysis. No external repository is
executed and no GitHub write is enabled.

The SDK is pinned to 0.23.1. Installation, backend tests, and native-state
restoration have been exercised on Windows with Python 3.14.3. UI tests cover
desktop and mobile workflows. See the root README for startup and validation.

## Confirmed Foundation

- Python backend using the Python OpenAI Agents SDK and native handoffs.
- Single-user WebUI; reminders and human reproduction results stay in the WebUI.
- Local Docker execution for Python, Go, and TypeScript target repositories.
- Automatic project setup with WebUI configuration fallback.
- Daily checks, initially sized for three repositories.
- Personal reproduction for system-discovered findings before Issue submission or repair.
- Existing Issues use automatic validation without a human reproduction checkpoint.
- Automated fork, issue, repair, verification, and PR preparation afterward.
- Project owners review and merge; this system never executes merges.
- Windows first, Linux deployment later.

## Shared Memory Increment (2026-10-10)

Implemented after milestone one: shared, repository-and-revision-scoped
SQLite memory; owner knowledge management; evidence-backed fixture experience;
bounded per-role context selection; immutable read/version audit; WebUI search,
filters, edit/history/archive/restore; additive schema and one-time evidence
backfill. Existing confirmation gates and SDK checkpoint compatibility remain.

This does not enable live models, real repository execution, semantic retrieval,
or automatic cross-version reuse. See ADR 0012 for contracts and limits.

This plan sequences the confirmed requirements. An intermediate milestone is
not a reduction of the complete version-one scope.

## Origin-Dependent Approval Increment (2026-10-10)

Implemented for controlled fixtures: immutable backend-owned origins, automatic
validation of a trusted existing-Issue sample, conditional SDK approval using the
existing tool identity, source-aware repair/review gates, distinct automatic
memory evidence, WebUI notifications for paused validation, and additive migration.
Old discoveries still require personal reproduction; existing checkpoints restore.
Both paths require passing patch checks before a PR draft and completion.

Real GitHub Issue intake is not implemented. A later trusted intake adapter must
verify Issue identity and repository membership, deduplicate against system-origin
findings, and preserve the original gate when a discovered finding gains an Issue.
Model output or a browser-provided origin is never sufficient provenance.
See ADR 0013, which supersedes universal human-reproduction wording below.

## GitHub Account Binding Increment (2026-10-12)

Implemented on `codex/github-account-binding`: PAT-based account connections,
GitHub-verified user identity, multiple saved identities with one active account,
revalidation on switch, token rotation, optimistic-version protected changes,
and local disconnect. Read-only checks verify access to registered repositories
without claiming Issue/PR write permission.

Tokens are encrypted in SQLite using a per-installation key kept in the operating
system credential vault. The UI and API never return tokens, ciphertext, or keys.
Mock HTTP integration and browser tests do not use personal credentials.
This does not add OAuth, automatic CLI-account import, repository execution,
or publication. See ADR 0014 for storage and migration contracts.

## Initial Read-Only Host Check

Observed on 2026-10-09 without installing software or starting containers:

| Component | Observation |
| --- | --- |
| Python | The default interpreter reports Python 3.14.3. |
| Git | Reports 2.54.0.windows.1. |
| Node.js | Reports v24.14.0. |
| Docker CLI | Reports 29.4.1 with the desktop-linux context. |
| Docker engine | Unreachable: the Docker Desktop Linux engine named pipe was not found. |
| npm | Available on PATH; package installation has not been tested. |

Finding the Docker CLI does not establish a working engine. Do not run
repository containers until the engine can be contacted and the selected
execution policy has been verified.

At that initial check, dependency compatibility had not yet been validated.
Milestone one subsequently installed and tested the Python/SDK and frontend
dependencies. Provider credentials, GitHub identity, Docker images, and runtime
resource limits remain outside this milestone. Docker engine availability
has not been resolved by this delivery.

## Milestone 1: Durable Human Checkpoint And Native Handoffs

- Establish the application structure and select remaining web-service,
  frontend, persistence, and task-dispatch components.
- Register real SDK specialists, their permitted handoff targets, and scoped
  tools. Keep application authorization outside model instructions.
- Build a fixture-backed finding flow with a real SDK approval interruption.
- Persist the business record and resumable SDK state; release the worker.
- Record a user result through the WebUI and resume only the matching finding.

Completion checks:

- The active SDK specialist changes through a native handoff.
- A process restart does not lose pending reproduction work.
- A confirmed finding resumes; a negative or absent confirmation does not.
- Repeated confirmation does not schedule duplicate follow-up work.
- For discoveries, no Issue publication or repair is possible before confirmation.
- For existing Issues, automatic validation is required before repair.

Use controlled fixtures and simulated tool/provider responses where suitable;
no real GitHub write is needed to establish these boundaries.

## Milestone 2: Local Project Execution And Personal Reproduction

Implemented on `feature/repository-execution` (2026-10-12): exact-commit source
archives, authenticated downloads, durable environment operations, restricted
Docker execution, Python/Go/npm command discovery, WebUI command overrides,
on-demand isolated preview, stop/restart and explicit cleanup. Docker Desktop's
Linux engine was started for authored-fixture validation. See ADR 0015 for
network, credential, runtime, retention and source-format limits.

This supplies environments for personal operation. Real findings and their
revision-bound confirmation workflow remain milestone three; the existing
fixture confirmation flows are unchanged.

- Verify Docker engine readiness and implement the restricted execution policy.
- Check out an authorized repository revision into a task-specific workspace.
- Add Python, Go, and TypeScript command discovery and runtime support.
- Display failed setup and user-provided command overrides in the WebUI.
- Stop waiting project containers and restart recorded environments on demand.
- Present project access, readiness, reproduction instructions, and diagnostics.

Completion checks:

- Supported-language fixtures can install, start, and test in their environments.
- Failed setup is visible and is not mislabeled as a verified project bug.
- Reproduction can be performed after a project environment has been stopped.
- Project code cannot obtain publishing credentials or Docker management access.

## Milestone 3: Discovery, Repair, And Independent Review

Analysis increment (2026-10-12): automatic queueing after test/start results,
encrypted WebUI provider settings, immutable bounded source/record snapshots,
four native SDK roles with read-only tools, evidence-checked findings, repository
memory references, WebUI notices and personal reproduction feedback are implemented.
Live provider acceptance remains separate. The repair/publication increment below
extends confirmed discoveries into the workflow described in ADR 0018.

Environment-diagnosis increment (2026-10-12): failure snapshots, local rules,
optional live-model diagnosis with three native SDK roles, evidence validation,
and an explicit version-bound configuration-apply/reinstall action are implemented.
This diagnoses environment failures only; discovery and repair below remain
planned. See ADR 0016 for provider configuration and data boundaries.

- Produce structured findings and evidence with revision identifiers.
- Deduplicate findings against prior runs and known work.
- Route discoveries through their own human checkpoint and existing Issues through
  automatic validation; pause unsuccessful validation without inventing owner approval.
- After the origin-specific gate, hand off to repair and independent review specialists.
- Preserve pre-repair evidence and assess regression results.
- Bound repair/review cycles and expose unresolved failures.

Automatic repair increment (2026-10-14): after a confirmed discovery, the
durable queue can run the repair agent and independent reviewer, validate a
new regression against the original and patched source, run the full suite,
and publish a marked Issue and PR through the trusted REST adapter. The
installation switch is disabled on upgrade; once enabled there is no per-
finding approval. Review vetoes and failed tests are retryable from a fresh
attempt, while a verified publication retry reuses the tested patch. Remote
operations are reconciled before retrying uncertain writes. The repository
owner remains the only merge actor. Real existing-Issue intake is not part
of this increment.

Completion checks:

- Multiple findings and repositories can make progress independently.
- An agent cannot unlock the checkpoint through text or handoff metadata.
- Changes that invalidate reproduction evidence do not silently reuse approval.
- A patch is not accepted merely because it removes or weakens a failing test.

## Milestone 4: GitHub Contributions

The controlled REST publication implementation is in place (2026-10-14).
HTTP mocks cover direct push, public fork fallback and uncertainty recovery.
Live contribution acceptance against a designated repository is still pending.

- Configure the contribution identity and validate actual repository permissions.
- Implement permission-checked fork, branch, issue, and PR operations.
- Enforce origin-specific reproduction and verification gates at the tool boundary.
- Store remote identifiers and reconcile uncertain outcomes before retries.
- Display PR status without exposing merge or auto-merge operations.

Completion checks:

- Fixtures and mocked APIs cover duplicates, denied permissions, and retries.
- Live integration tests use an explicitly designated test repository.
- Tool calls cannot bypass reproduction or publish an unreviewed patch.
- Owner review and merge remain external to this system.

## Milestone 5: Daily Operation And Migration

- Add persistent daily scheduling, overlap handling, and per-repository pause.
- Keep reproduction reminders in the WebUI and avoid duplicate reminders.
- Record usage while leaving the initial spending cap disabled.
- Apply resource, timeout, retry, and retention policies.
- Verify restart recovery and Windows-to-Linux migration of pending work.
- Document host availability requirements and protected WebUI access.

Completion checks:

- A scheduled run cannot treat elapsed time as human confirmation.
- Offline periods and restarts cannot duplicate issues, PRs, or confirmations.
- Three registered repositories can complete the expected daily workflow.
- Pending findings and registered repositories survive a migration rehearsal.

## Decisions Still Needed Before Their Implementation Stage

- SDK/runtime pins, selected model endpoints, and provider compatibility.
- Web-service and frontend stack, durable storage, and task dispatch.
- GitHub account setup and credentials, without collecting secrets in chat.
- WebUI authentication, listening address, and future remote-access protection.
- Daily execution time and timezone.
- Required reproduction evidence and confirmation invalidation rules.
- Runtime limits, retained data, environment inactivity behavior, and cleanup.
- Confidential vulnerability handling before any public contribution.

The user subsequently authorized milestone-one implementation. That
authorization does not enable running untrusted repositories or publishing
live GitHub contributions before the relevant later-stage controls are built.
