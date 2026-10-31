# Multi-Agent GitHub Maintenance System

Status: local execution, real Issue intake, daily checks and controlled repair/publication
implemented; central coordination is in staged development
Updated: 2026-10-31

## Goal

Design a multi-agent system that analyzes GitHub repositories, identifies
project problems, opens issues, fixes bugs, and submits pull requests.

## Current Status

Issue #112 is implemented on `feature/scope-review-recovery-112` (base
`5c25840`): final-review validation now applies the same exclusion-evidence
semantics as persistence, malformed correction structures fail closed, mapping
and scope-review retry signatures include the current checkpoint, and repeated
unchanged failures stop with a text-free fingerprint. Public diagnostics remain
bounded, expose only registered scope keys or hashes, and preserve the recovery
identity fields. Targeted validation passed 74 backend cases; Ruff, Python
compilation and diff checks also passed. The branch has not yet received final
independent approval, so no PR has been created. It is not merged or deployed,
the production workbench on port 8765 has not been restarted, and the original
real analysis has not yet been rerun against this branch.

Issue #105 is implemented on `feature/correction-storage-105` (base `96f5105`):
the correction-state INSERT explicitly binds six columns to six parameters;
SQLite database failures have a fixed analysis error code and numeric-only
diagnostics. Review and validation status are recorded in
[storage fix](correction-storage-105.md). PR #106 was open when deployed:
on 2026-10-30 the reviewed feature commit `6db43a7` was started at local port
8765 with the original database after a consistent backup. Startup, session
API, served assets and original record counts were checked. This is a branch
deployment; actual correction/recovery acceptance remains outstanding.

Issue #103 is implemented on `feature/evidence-recovery-103` (base `d22226a`):
private unaccepted correction candidates survive interrupted evidence/scope
correction requests, with checkpoint bindings, freshly computed errors and
metadata-only quote comparisons. Static checks and frontend build passed; no
test cases or runtime acceptance were run before delivery. PR #104 was merged
as `96f5105` on 2026-10-30, including PR #102, and started at local port 8765
with the original database after a consistent backup. Startup, session API,
served asset hashes and record counts were checked. A subsequent real analysis
failed while saving its correction candidate; Issue #105 addresses that defect.
Startup did not establish acceptance of the correction recovery path.
See [correction recovery](evidence-recovery-103.md).

Issue #101 is implemented on `feature/unlimited-workflows-101`, based on
`d22226a`: workflow request/token/correction/resumption/duration quotas are
removed, public limits are null, and patch policy permits new production files,
existing tests, dependency/build manifests and CI without a 32KB file ceiling.
Usage, finite renewable leases, single-request timeouts, cancellation, evidence,
review and publication authority remain enforced. See [current policy](unlimited-workflows-101.md)
and [review](tasks/0101/review.md). Static checks and frontend build are recorded;
no test cases or live-model/Docker acceptance were run for this increment.
Delivery PR #102 was merged as `fd42113` on 2026-10-30 and included in the
local `96f5105` startup; no full workflow acceptance was run during restart.
Earlier quota and patch-allowlist descriptions
below document the preceding policy and are superseded by this increment.
The coordinator still cannot independently invent a new analysis-retry action;
removing quotas alone does not establish a fix for invalid source quotations.

Issue #99 is implemented on `feature/unified-issues-99` (base `7b79e8e`): the
repository flow has five pages, and Issue combines GitHub issues with local
analysis findings, source filters, evidence and disposition. Reports remain in
analysis; patches, tests and independent review remain in repair. The legacy
`reproduction` section maps to Issue. Validation and delivery status are recorded
in [unified Issue](unified-issues-99.md). PR #100 was merged as `d22226a` on
2026-10-29 and started at local port 8765 using the original database after a
consistent backup. Startup and served asset bytes were checked; no test suite
or live-model acceptance was run during restart, and managed release supervision
was not enabled. That startup did not validate the #101 or #103 increments.

The local single-user workbench supports fixed-revision Docker execution, model-based
analysis, GitHub Issue synchronization and Issue-based analysis, daily repository
checks, and controlled repair and publication. Issue #97 implements evidence-bound
repair candidates without personal reproduction confirmation on its feature
branch. Existing Issues retain their validation and evidence gates. Offline
verification and diff review are recorded in [candidate eligibility](finding-repair-eligibility-97.md)
and [review](tasks/0097/review.md). Delivery PR #98 was merged as `7b79e8e`
on 2026-10-29 (Asia/Shanghai). Its code was started in the local workbench at
port 8765 with the original database after a consistent backup. Startup and
served frontend bytes were verified; no test suite or live-model acceptance was
run during restart. This manual startup did not enable managed release supervision.
Ordinary repository merges remain owner-operated. The self-maintenance
release CLI described below has separate, explicit owner authorization; no
background worker can independently merge or deploy.
Merged repair experience can be written back to repository memory after owner
disposition and merge verification. See the [README](../README.md) and the later
ADRs for current behavior and limitations.

The main discovery-to-publication path has been accepted on Windows with a real
model and real GitHub writes. This does not validate every provider, repository
or operating system; independent live-provider diagnosis and Linux acceptance
remain outstanding. Scope acceptance has additional limits described in the README.

Central coordination is a separate six-stage effort (#77). Stage 1, unified fault
recording (#78), was merged in PR #84 on 2026-10-28; recording alone does not
execute recovery. Stage 2 (#79) is implemented and verified on
`feature/workflow-recovery-79`, based on `ba043b1`: shared ownership and budgets,
atomic scope batches and opt-in rule recovery (disabled by default). Final backend
regression: 1088 passed, 0 failed, 12 opt-in Docker cases skipped; browser checks:
4 passed; frontend build, Ruff and independent review passed. Delivery PR #90 was
merged as `367baff` on 2026-10-28; deployment remains unverified.
Stage 3 (#80) is implemented on `feature/system-coordinator-80`,
based on `367baff`: independently configured coordinator, evidence-bound plans,
server-authorized actions, bounded fallback, progress and deduplicated notifications.
Targeted validation and independent review passed: 24 coordinator cases, one
real-source adapter recheck, 59 related cases from the earlier check, one browser
smoke, frontend build and Ruff. Per owner request, no new full regression was run.
PR #91 was merged as `74b0918` on 2026-10-29 (Asia/Shanghai); deployment is unverified. See the
[stage 3 policy](system-coordinator-80.md) and [validation](system-coordinator-80-validation.md).
Stage 4 (#81) is implemented on `feature/system-maintenance-81`, based on `74b0918`.
It adds separately authorized, evidence-bound RepoWatch maintenance through a
Chinese Issue, isolated feature workspace, verified patch, independent review
and Chinese PR. Targeted validation passed: 13 backend cases, one desktop/mobile
browser smoke, frontend build and Ruff; independent review passed. No full
regression was run, per owner request. PR #92 was merged as `172c5a8` on
2026-10-29 (Asia/Shanghai); deployment is unverified. Automatic patches cover Python implementation
and new Python regressions only; see the [policy](system-maintenance-81.md) and
[validation](system-maintenance-81-validation.md).
Stage 5 (#82) is implemented on `feature/workbench-release-82`, based on the
latest default commit `172c5a8`: exact-target merge/deploy grants, immutable
runtime bindings, draining and backup-first migration probes, real asset and
original-DB readiness, independent bounded supervision, offline event delivery,
data-preserving code rollback and source-owned original-step resumption.
Eight focused backend cases, one desktop/mobile browser smoke, build, Ruff and
independent review passed. The native-process release/restart/rollback drill
passed. PR #93 was merged as `599de39` on 2026-10-29 (Asia/Shanghai);
production deployment and enablement remain unverified. No full regression was
run, per owner request. See the [release policy](workbench-release-82.md) and
[validation](workbench-release-82-validation.md).
Stage 6 (#83) implemented an offline cross-stage journey, a fixed 16-case
acceptance runner, portable evidence, staged operations and narrowly bound
cross-version source verification on `feature/central-acceptance-83`.
All 16 focused cases, Ruff and independent code/documentation review passed.
PR #94 was merged as `8930246` at 2026-10-29 15:39:59 (Asia/Shanghai), closing
#83. This does not establish production deployment or enablement; parent #77
remains open. See the [acceptance matrix and limits](central-acceptance-83.md).
The whole-chain review found four closure defects and 12 stale test-contract
failures, tracked by repair sub-Issue #95 under #77.
Those fixes are implemented on `feature/central-review-fixes-95` from `599de39`:
role-scoped maintenance evidence (with fail-closed legacy digest compatibility),
source-verified coordinator/notification reconciliation, identity-checked failed
first-deploy cleanup, and bounded original-task verification. The release-source
proof from PR #94 is reused and extended to accept authorized success produced
within the verification window even when consumed after timeout; it does not
relax account, repository, environment, revision or model checks. No schema or
budget reset is introduced. Targeted validation and diff self-review passed;
the repair is not merged or deployed. PR #96 now integrates default-branch
commit `8930246`, preserving the stage 6 runner and the stricter blocked-state,
original-window and per-release authorization checks. Conflict resolution does
not change the running workbench, its database or permissions. See the
[repair validation](central-review-fixes-95-validation.md) and the
[stage 2 policy](workflow-recovery-79.md) and [validation](workflow-recovery-79-validation.md).
Merge does not establish deployment to the running workbench. See
[stage 1 validation](system-fault-records-78-validation.md).

Issue #88 aligns the repair/review regression fixtures and assertions with the
existing backend-controlled SDK handoff and evidence-read contract. On
`feature/main-regression-88`, backend regression verification reports 1061 passed,
0 failed and 12 skipped (opt-in real Docker integration tests). Production logic
is unchanged; PR #89 was merged into the default branch as `ba043b1` on
2026-10-28. An additional Docker run passed 11 cases with one cached-desktop
case skipped, giving 1072 passed, 0 failed and 1 skipped across that validation.
Deployment to the running workbench has not been verified.
See [the regression validation](main-regression-88-validation.md) for the original
35 failures, preserved negative gates and acceptance limits.

## Confirmed Requirements

- Version one accepts a user-provided GitHub repository URL as its entry point.
  Any repository accessible to the system is eligible, not only repositories
  owned by the user. Access must be verified using the configured identity.
- The system downloads repositories into local workspaces on its execution
  host and runs them as part of analysis. Projects use separate local Docker
  containers for dependency installation, execution, and testing, with
  restricted mounts and permissions. Detailed isolation policy remains to
  be specified; local execution does not imply unrestricted host access.
- Analysis and finding discovery are automated. Under change #97, findings retained
  by a valid final independent review become `repair_candidate` records. When
  automatic repair is enabled, current eligible candidates proceed without
  personal reproduction feedback. Reproduction steps remain available.
- Trusted server provenance binds the finding, final analysis, revision,
  configuration and execution basis. Existing genuine owner confirmations remain
  readable; old waiting records require current analysis or validated continuation.
- Existing GitHub Issues retain their trusted intake origin and automatic
  validation requirements. Model output cannot change that origin.
- Users may dispose candidates as not reproduced, false positive or resolved.
  Closing a finding blocks later actions; an already sent remote request keeps its
  actual receipt. Project containers need not remain active for disposition.
- Issue creation, repair branches and PR publication require current eligibility,
  regression failure before the patch, passing regression and full tests after it,
  independent repair review, budget and account checks.
- Ordinary repository repairs require project-owner review and merge.
  The self-maintenance release increment adds only explicitly owner-invoked,
  exact-HEAD authorized merges under GitHub rules; it does not enable background
  merging, bypass PR review or confer deployment permission.
- Automated tests and agent review do not confer project-owner merge or deployment permission.
- The system may automatically fork a selected repository into the configured
  GitHub account and use that account to submit issues and pull requests,
  subject to available permissions and repository restrictions.
- Automatic issue publication for discoveries requires current evidence-bound
  eligibility. Repair PRs require origin-specific validation, passing repair tests
  and independent review. Refactoring suggestions,
  style findings, and unverified suspected problems remain in the analysis
  report.
- The system supports recurring scheduled repository checks after intake.
  Daily checks use each repository's configured local time.
- Version one runs on the user's current Windows host. A later deployment
  will migrate to a Linux server; portability is a design requirement.
- The user imposes no model-provider or model-family restriction. Specific
  models remain architectural choices; this does not
  establish that any provider credentials are already configured.
- Implement the agent backend in Python using the Python OpenAI Agents SDK,
  including native handoffs for responsibility transfers between agents.
  The current SDK is pinned to 0.23.1; see the project configuration for runtime requirements.
- The current budget policy has no daily model-call spending cap across
  repositories. This is the initial configuration preference, not a permanent
  requirement to prohibit budget limits.
- The user-facing interface is a WebUI for repository intake, run progress,
  and analysis reports.
- The WebUI supports multiple saved GitHub personal-account connections with one
  explicitly selected current account. Identity is verified against GitHub.
  Tokens are encrypted and kept outside model context, memory, and API responses.
  This connection identity is independent of the developer's GitHub CLI login.
- Findings requiring the user's reproduction are announced in the WebUI
  only. Email and external messaging notifications are not required.
- Version one serves a single user. Team roles and multi-user collaboration
  are outside the initial scope; authentication and network exposure still
  remain important boundaries; the current service is local-only.
- The expected initial workload is three registered repositories checked
  daily. This is a sizing assumption, not a hard repository-count limit or
  a decision to execute three runs concurrently.
- The initial repositories have not been selected. Repository-specific
  validation is deferred; URL-based intake remains generic.
- Version one explicitly targets Python, Go, and TypeScript projects.
  The user's mention of additional languages leaves their exact scope open;
  universal language or project compatibility is not a confirmed requirement.
- The system first attempts to discover and execute each project's dependency
  installation, startup, and test commands. If automatic setup or execution
  fails, the user supplies or corrects configuration through the WebUI.
  These commands execute in the project's Docker environment, subject to
  the detailed isolation policy.

These requirements do not authorize modification of any particular repository.

The reproduction checkpoint is mandatory for system discoveries. Existing
Issues are exempt from human reproduction, not from automatic validation or
the subsequent test, review, and publication permission gates.

## Workflow Boundary

System discovery: repository URL -> local checkout and execution -> automated analysis and
finding discovery -> wait for human reproduction confirmation -> issue
submission -> repair and verification -> PR submission -> project-owner
review and merge.

Existing Issue: verified Issue intake -> local checkout and automatic validation
-> repair and verification -> PR submission -> project-owner review and merge.
No duplicate Issue is created. Unsuccessful automatic validation pauses.

This is a requirements-level sequence, not a finalized implementation state
machine. Agent-assisted evidence collection is allowed before the human
checkpoint but cannot satisfy it on its own.

## Open Questions

This section preserves the original design questions as a historical checklist.
Some have since been answered or implemented; current decisions and remaining
limits are recorded in the README and subsequent ADRs.

- What discovery evidence and execution checks should be used for project
  commands, and how should WebUI overrides be validated and retained?
- Which runtime versions, package managers, application types, and external
  service dependencies should version one support?
- Which additional languages should be supported later, and how should
  unsupported project environments be reported?
- Which GitHub identity and authentication method will the system use?
- What is the fallback when a repository allows analysis but not issue
  creation, forking, or pull-request submission?
- What qualifies as sufficient human reproduction evidence for a bug?
- How should the WebUI handle unsuccessful reproduction, rejection, and
  findings awaiting user action?
- What project state must be retained to restart reproduction environments
  on demand, and how long should retained files and data be kept?
- How is confirmation tied to the repository revision and finding, and what
  changes invalidate it?
- How are project-owner review and merge status displayed without granting
  the system merge execution responsibility?
- What verification is required before publishing a finding or a patch?
- Docker CLI is present on the current host, but its Linux engine was
  unreachable during the 2026-10-09 readiness check. What setup is needed
  to make it usable, and what execution resources are available?
- How should container images, mounts, permissions, network access, resource
  limits, and credentials be managed?
- Which Python runtime and Python Agents SDK package versions should be pinned?
- Which models and provider adapters should be configured, and which provider
  credentials and capability checks are required?
- What execution time and timezone should daily checks use?
- Should unchanged repositories be reanalyzed, and how are overlapping
  scheduled runs handled?
- What are the per-repository timeout, retry, and concurrency limits?
- How should model usage and cost be recorded when no spending cap is set?
- What authentication and network-access boundaries should protect the
  single-user WebUI on Windows and after Linux migration?
- How should the WebUI present failures and offer pause, retry, and stop actions?
- How are repository code execution, credentials, and untrusted content isolated?
- How are duplicates, retries, failures, and operator stop requests handled?

## Design Records

- [Proposed architecture](architecture-draft.md)
- [Implementation plan and host readiness](implementation-plan.md)
- [ADR 0001: Approval boundary](adr/0001-approval-boundary.md)
- [ADR 0002: URL-based repository intake](adr/0002-repository-intake.md)
- [ADR 0003: Automated fork-based contributions](adr/0003-fork-contributions.md)
- [ADR 0004: Reproducible-bug publication scope](adr/0004-finding-publication-scope.md)
- [ADR 0005: Scheduled repository checks](adr/0005-scheduled-checks.md)
- [ADR 0006: Local Windows deployment, later Linux migration](adr/0006-deployment-portability.md)
- [ADR 0007: Local execution and mandatory human reproduction](adr/0007-human-reproduction-local-execution.md)
- [ADR 0008: Automatic project setup with WebUI fallback](adr/0008-project-setup.md)
- [ADR 0009: Local Docker project execution](adr/0009-docker-execution.md)
- [ADR 0010: Stopped waiting environments and WebUI feedback](adr/0010-reproduction-feedback.md)
- [ADR 0011: OpenAI Agents SDK and native handoffs](adr/0011-agents-sdk-handoffs.md)
- [ADR 0012: Shared repository memory](adr/0012-repository-memory.md)
- [ADR 0013: Origin-dependent reproduction](adr/0013-origin-dependent-reproduction.md)
- [ADR 0014: GitHub account binding](adr/0014-github-account-binding.md)
- [ADR 0015: Fixed-revision repository environments](adr/0015-repository-execution.md)
- [ADR 0016: Execution failure diagnosis](adr/0016-execution-diagnosis.md)
- [ADR 0017: Automatic repository analysis](adr/0017-repository-analysis.md)
- [Domain glossary](glossary.md)

## Interview Workflow

The requested grill-with-docs skill delegates to grilling and domain-modeling.
Those dependencies were not found in the inspected local skill directories.
Continue with focused requirements questions and explicit decision records;
do not treat missing skill instructions as loaded.
