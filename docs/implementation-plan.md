# Implementation Plan

Date: 2026-10-09
Status updated: 2026-10-31

## Current Delivery Status

- Issue #108 was merged into the default branch via PR #109 as merge commit
  `caa53fa` on 2026-10-31. Default-off flash-monitor / v4pro-repair routing,
  immutable repair-phase snapshots, provider reference protection and
  escalation failure audit are in the default branch. The real
  target-repository acceptance is still pending. See
  [review](tasks/0108/review.md).

- Issue #114 was merged into the default branch via PR #115 as merge commit
  `0c4affe` on 2026-10-31. Source screening no longer treats short `sk-` labels
  or placeholders as credentials, while log redaction, configured secrets,
  credential assignments, URL userinfo and realistic `sk-` keys remain
  blocked. Focused validation and independent review passed with only
  documented P3 residual risks; deployment and real target-repository
  acceptance remain outstanding.

- Issue #116 was merged into the default branch via PR #117 as merge commit
  `608d30c` on 2026-10-31. Request timeouts have a persistent four-retry
  budget; tool, handoff and required-read corrections share a persistent
  two-correction budget; terminal manual analyses are superseded by a fresh
  record; and environment stop/delete cancels orphan analyses with their lease,
  recovery state and audit event. Exhausted timeout budgets also stop automatic
  report recovery. Final split regression passed 456 cases, including 43 system-fault
  cases. One execution-pipeline timing case initially exceeded its three-second
  wait under parallel load; the complete execution-pipeline file was rerun
  serially and passed 23/23. Ruff, Python compilation, frontend build and diff
  checks passed. Code review has no blocking findings. Deployment, workbench
  restart and real target-repository acceptance remain outstanding. See
  [review](tasks/0116/review.md).

- Issue #112 was merged into the default branch via PR #113 as merge commit
  `0c771ae` on 2026-10-31. It aligns final-review and save-time scope exclusion
  validation, makes malformed scope structures fail closed, binds retry
  signatures to the current checkpoint, and stops deterministic no-progress
  recovery with bounded, text-free failure events. Targeted validation passed
  74 backend cases; Ruff, Python compilation and diff checks passed. Final
  independent review passed with no blocking findings, including a real
  save-conflict recovery regression. It is not deployed, port 8765 has not been
  restarted, and the real analysis replay remains pending.

- Issue #105 is implemented on `feature/correction-storage-105` (base `96f5105`):
  fix correction-state INSERT arity and classify SQLite database failures with
  numeric-only diagnostics. See [delivery and review](correction-storage-105.md).
  PR #106 was open when feature commit `6db43a7` was deployed to the local
  workbench on 2026-10-30. Database backup, startup, session API, served assets
  and preserved record counts were checked. Historical failure state remains;
  actual correction/recovery acceptance has not been performed.

- Issue #103 is implemented on `feature/evidence-recovery-103`, based on
  `d22226a`: persist unaccepted evidence/scope correction candidates and resume
  the same correction with fresh validation and safe quote diagnostics.
  Static checks and frontend build passed before delivery. PR #104 was merged
  as `96f5105` on 2026-10-30 and started at port 8765 with the original database
  after backup. Startup, session API, served assets and record counts were checked;
  no test cases or end-to-end recovery acceptance were run during restart.
  A subsequent real analysis failed saving its correction candidate; #105 tracks
  that implementation defect. See [delivery](evidence-recovery-103.md).

- Issue #101 is implemented on `feature/unlimited-workflows-101` (base `d22226a`):
  remove cumulative workflow quotas and broaden patch permissions, with actual
  usage, renewable ownership, cancellation, single-request timeouts and evidence/
  review gates retained. Static checks and build are recorded in
  [review](tasks/0101/review.md); no test cases, paid model or Docker acceptance
  were run. Delivery PR #102 was merged as `fd42113` on 2026-10-30;
  its code was included in the `96f5105` workbench startup, with full workflow
  acceptance still outstanding. See
  [policy and limitations](unlimited-workflows-101.md), which supersedes earlier
  finite-quota and Python-only patch descriptions in this plan.

- Issue #99 is implemented on `feature/unified-issues-99`, based on `7b79e8e`:
  five repository pages, a combined GitHub Issue/analysis-finding page with
  source filters, and preserved analysis selection, dispositions and repair
  navigation. PR #100 was merged as `d22226a` on 2026-10-29 and started
  at port 8765 with the original database after a consistent backup. Startup and
  served assets were checked; no test suite/live-model acceptance was run during
  restart, and managed release supervision was not enabled. See
  [delivery and validation](unified-issues-99.md).

- Issue #97 is implemented on `feature/remove-manual-reproduction-97`, based
  on `f8e91ed`: valid analyzed findings become automatic repair candidates without
  per-finding personal confirmation. Offline verification and diff review are recorded; no merge
  or deployment was claimed at PR creation. Delivery PR #98 was subsequently
  merged as `7b79e8e` on 2026-10-29 (Asia/Shanghai) and started in the local
  workbench at port 8765 with the original database after a consistent backup.
  Startup and served frontend bytes were verified; no test suite or live-model
  acceptance was run during restart, and managed release supervision was not
  enabled. See [policy](finding-repair-eligibility-97.md).

- The original execution, analysis, controlled repair and GitHub publication
  increments are implemented. Real GitHub Issues can be synchronized and used
  for analysis; daily repository checks and their history are available.
- The Windows main path has undergone real-model and real-GitHub-write acceptance.
  This is not universal provider or repository acceptance; independent live
  diagnosis and Linux migration acceptance remain outstanding.
- Central coordination (#77) is a separate six-stage effort. Unified fault
  recording (#78) was merged via PR #84 on 2026-10-28. Automatic recovery (#79)
  is implemented and verified on `feature/workflow-recovery-79`
  (base `ba043b1`), merged in PR #90 as `367baff` on 2026-10-28; deployment unverified.
  Final backend regression: 1088 passed,
  0 failed, 12 opt-in Docker cases skipped; browser checks: 4 passed. Frontend
  build, Ruff and independent review passed. New rule recovery is disabled by
  default. See the [policy](workflow-recovery-79.md)
  and [validation](workflow-recovery-79-validation.md). The coordinator (#80) is
  implemented on `feature/system-coordinator-80` (base `367baff`). Targeted
  validation and independent review passed: 24 coordinator cases, a real-source
  adapter recheck, 59 earlier related cases, one browser smoke, build and Ruff.
  No new full regression was run, as requested by the owner. PR #91 was merged
  as `74b0918` on 2026-10-29 (Asia/Shanghai); deployment is unverified. See the
  [coordinator policy](system-coordinator-80.md) and
  [validation](system-coordinator-80-validation.md).
  Self-maintenance (#81) is implemented on `feature/system-maintenance-81`
  (base `74b0918`): dedicated maintenance ledger, fixed repository/identity,
  evidence-based Issue/PR reuse, isolated feature workspaces and existing
  verification/review gates. Targeted validation passed: 13 backend cases,
  one desktop/mobile browser smoke, frontend build and Ruff. Independent review
  passed; no full regression was run, as requested. Chinese delivery PR #92 was
  merged as `172c5a8` on 2026-10-29 (Asia/Shanghai); deployment is unverified.
  The automatic patch boundary is Python implementation
  and new Python regressions. See the [policy](system-maintenance-81.md) and
  [validation](system-maintenance-81-validation.md).
  Deployment/service recovery (#82) is implemented on
  `feature/workbench-release-82`, based on `172c5a8`: separate exact-target
  merge/deploy grants, pinned manifests, draining/backup/migration probes,
  runtime/assets/original-DB checks, bounded independent supervision, offline
  event delivery, data-preserving rollback and original-adapter resumption.
  Eight focused backend cases, one desktop/mobile smoke, frontend build,
  Ruff and independent review passed. The native-process release/restart/rollback
  drill passed; PR #93 was merged as `599de39` on 2026-10-29 (Asia/Shanghai).
  Production deployment and default enablement remain unverified.
  No full regression was run.
  See the [release policy](workbench-release-82.md) and
  [validation](workbench-release-82-validation.md).
  Full acceptance (#83) implemented a cross-stage offline journey, a fixed
  16-case runner, portable evidence, a specification matrix, staged operations
  and precise cross-version source verification on `feature/central-acceptance-83`.
  All 16 focused cases, Ruff and independent code/documentation review passed.
  PR #94 was merged as `8930246` at 2026-10-29 15:39:59 (Asia/Shanghai), closing
  #83. Production deployment and enablement are not established; parent #77
  remains open. See the [matrix and limits](central-acceptance-83.md).
  Whole-chain review found four closure defects and 12 test-contract failures.
  Repair sub-Issue #95 under #77 is implemented on
  `feature/central-review-fixes-95` from `599de39`; targeted validation and diff
  self-review passed, not merged or deployed. It fixes maintenance evidence
  invalidation, coordinator/notification closure, failed first-deploy cleanup
  and original-verification termination, and adapts the two stale test fixtures.
  The release-source proof from PR #94 is reused with bounded, authorization-
  checked delayed-success handling. PR #96 now integrates main commit `8930246`
  with the stage 6 runner and stricter blocked-state, original-window and
  per-release authorization checks preserved. The repair remains unmerged and
  undeployed; conflict resolution does not change the running workbench. See
  [the repair record](central-review-fixes-95-validation.md).
  A merged change is not proof
  that the running workbench has been deployed.
- Stage 1 validation recorded 1024 passing, 35 failing and 12 skipped backend
  tests. The same 35 failures appeared on its unmodified base; no new failures
  were identified in that comparison. That historical run was not green.
  See [the validation record](system-fault-records-78-validation.md).
- Issue #88 corrects stale repair/review test contracts and fixtures on
  `feature/main-regression-88`. The full backend run now reports 1061 passed,
  0 failed and 12 skipped; the skipped cases require explicit real Docker
  integration opt-in. Production behavior and recovery budgets are unchanged.
  PR #89 was merged as `ba043b1` on 2026-10-28. A subsequent real Docker run
  passed 11 cases, leaving one cached-desktop case skipped: 1072 passed, 0 failed,
  1 skipped across that validation. Production deployment remains unverified.
  See [the regression validation](main-regression-88-validation.md).
- Background automatic merging is not enabled. Ordinary repository owners
  merge on GitHub; the self-maintenance CLI requires explicit, separate,
  exact-HEAD merge authorization and GitHub rule approval.
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
- Project owners review and merge ordinary repairs. The self-maintenance CLI
  is a separate owner-invoked, exact-HEAD authorized exception under GitHub rules,
  not permission for background automatic merging or deployment.
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

Historical implementation baseline: the personal-confirmation gate below is superseded
by change #97; see the current status and candidate eligibility contract above.

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
