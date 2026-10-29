# Multi-Agent GitHub Maintenance System

Status: local execution, real Issue intake, daily checks and controlled repair/publication
implemented; central coordination is in staged development
Updated: 2026-10-29

## Goal

Design a multi-agent system that analyzes GitHub repositories, identifies
project problems, opens issues, fixes bugs, and submits pull requests.

## Current Status

The local single-user workbench supports fixed-revision Docker execution, model-based
analysis, GitHub Issue synchronization and Issue-based analysis, daily repository
checks, and controlled repair and publication. System discoveries still require
personal reproduction; existing Issues retain their own validation and evidence
gates. Ordinary repository merges remain owner-operated. The self-maintenance
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
passed. Chinese delivery PR #93 was merged as `599de39` on 2026-10-29
(Asia/Shanghai); production deployment and enablement are unverified. No full regression was
run, per owner request. See the [release policy](workbench-release-82.md) and
[validation](workbench-release-82-validation.md).
Stage 6 (#83) is implemented on `feature/central-acceptance-83`, based on
`599de39`: an offline cross-stage journey, a fixed 16-case acceptance runner,
portable evidence, staged enablement/rollback instructions and narrowly bound
post-release verification across workbench versions. All 16 focused cases,
Ruff and independent code/documentation review passed. Chinese delivery PR #94
is open; this branch is not merged or deployed. No production
database, permissions, live model or GitHub business writes were exercised.
The isolated scope and remaining live/native integration limits are explicit;
parent #77 stays open. See the [acceptance matrix](central-acceptance-83.md).
See the
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
- Analysis and finding discovery are automated. Each discovered suspected
  bug must then be reproduced and confirmed by a human before the system
  proceeds with issue submission, repair, or a repair PR for that finding.
- The single user personally operates the local project to reproduce the
  suspected bug, then records the result in the WebUI. Agent-generated
  evidence does not replace that user confirmation for system discoveries.
- Existing GitHub Issues do not require personal reproduction. The system
  automatically validates the issue at the task revision before repair.
  Failed validation pauses with a WebUI notification, without switching to a
  human reproduction requirement. Both origins still require tests and review.
- Origin is assigned by the trusted intake path and cannot be changed by model
  output, user-supplied metadata, or later Issue creation. Linking an Issue to a
  system discovery must not bypass that discovery's human checkpoint.
- Project containers do not stay running while waiting for the user to
  reproduce findings. The feedback interface must remain independent of
  the target project's container lifecycle.
- After human reproduction confirmation, issue creation, bug fixing,
  repair-branch pushes, and PR creation do not need separate per-action
  approval, subject to the remaining quality and permission gates.
- Ordinary repository repairs require project-owner review and merge.
  The self-maintenance release increment adds only explicitly owner-invoked,
  exact-HEAD authorized merges under GitHub rules; it does not enable background
  merging, bypass PR review or confer deployment permission.
- Automated tests and agent review cannot replace required human reproduction
  for system discoveries or project-owner review and merge for either origin.
- The system may automatically fork a selected repository into the configured
  GitHub account and use that account to submit issues and pull requests,
  subject to available permissions and repository restrictions.
- Version one automatically publishes new issues only for human-confirmed
  discoveries. Repair PRs require the origin-specific reproduction evidence
  plus passing repair tests and independent review. Refactoring suggestions,
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
