# Architecture Draft

Date: 2026-10-09
Status: first-stage SDK/WebUI implementation delivered; repository execution remains planned

## Implementation Note

Milestone one uses FastAPI, SQLite, React/TypeScript, and Python Agents SDK
0.23.1. It implements real SDK handoffs and persisted human approval through
six roles, using deterministic model responses and a trusted fixture. Docker
execution, live-model investigation, real GitHub publication, and daily
scheduling are not enabled. The broader architecture below remains the target.

Approval policy update, 2026-10-10: [ADR 0013](adr/0013-origin-dependent-reproduction.md)
supersedes universal human-checkpoint wording below. Discoveries require personal
reproduction; existing Issues require automatic validation instead. Both paths
retain patch tests and independent review. The current workbench exercises
both paths using trusted fixtures; real GitHub Issue intake is not implemented.

## Scope

The confirmed requirements are in [design.md](design.md). This draft proposes
component boundaries and responsibilities; it does not claim that software,
credentials, Docker, or repository environments have been installed.

Initial sizing is one user and three repositories, with daily checks.
Deploy locally on Windows first, then migrate to a Linux server.

## Components

| Component | Proposed responsibility |
| --- | --- |
| WebUI | Repository intake, project configuration, progress, findings, human reproduction results, and PR status. |
| Coordinator | Persistent business workflow, daily scheduling, bounded dispatch, human confirmation validation, and resumption of interrupted SDK runs. |
| Agent workers | Python OpenAI Agents SDK agents and its runner, using native handoffs for bounded responsibility transfers. |
| Project runner | Local checkouts and restricted Docker environments for installation, execution, and tests. |
| GitHub publisher | Permission-checked fork, branch, issue, and PR operations after applicable gates; no merge operations. |
| State and evidence store | Repository configuration, run history, finding evidence, user confirmations, publication identifiers, and audit records. |

A coordinator, runner, or publisher need not be a model-backed agent. Keep
authorization and workflow enforcement in application logic rather than
depending on agents to obey prompts.

## Proposed Agent Roles

These are task roles, not a requirement for six permanent processes or six
different model providers.

| Role | Output | Boundary |
| --- | --- | --- |
| Repository analyst | Project map and proposed install, startup, and test configuration. | Cannot grant execution permissions or silently bypass failed setup. |
| Problem investigator | Suspected bugs with code locations and observed behavior. | Cannot treat a suspicion as human-confirmed or publish it directly. |
| Reproduction assistant | Reproduction instructions and supporting runtime evidence. | Cannot impersonate the user or satisfy the human checkpoint. |
| Repair agent | A focused patch and proposed regression tests after confirmation. | Cannot start repairing an unconfirmed finding or merge changes. |
| Independent reviewer | Assessment of the patch, tests, and remaining risks. | Cannot replace user reproduction or project-owner review and merge. |
| PR author | A PR proposal with issue linkage, patch summary, and verification evidence. | Publishing tools enforce confirmation and review gates; no merge tool is exposed. |

Model selection can differ by role. The backend uses the Python OpenAI Agents
SDK; specific providers and pinned Python and SDK versions remain undecided.

## SDK Handoff Design

See [ADR 0011](adr/0011-agents-sdk-handoffs.md) for the selected framework and
official documentation references. This section is the proposed application
mapping, not an assertion that handoffs alone enforce a business workflow.

```text
Repository scan:
Repository analyst --handoff--> Problem investigator
  -> persist findings and dispatch independent per-finding work

Per-finding work:
Reproduction assistant
  -> persist evidence and request the human-reproduction checkpoint
  -> SDK interruption; save state and stop the waiting project environment
  -> user restarts the environment and reproduces the bug
  -> WebUI confirmation; application validates and resumes SDK state
  -> publish the issue through a gated tool
  --handoff--> Repair agent
  --handoff--> Independent reviewer
  --handoff--> PR author
  -> gated PR publication -> owner review and merge outside this system

Reviewer requests changes:
Independent reviewer --handoff--> Repair agent (bounded retries)
```

The scan-to-finding fan-out is application scheduling, not a single handoff
that is assumed to create parallel workers. Each finding has its own durable
checkpoint so another pending finding cannot block the entire repository.

Register only the intended handoff targets. Validate the current finding and
workflow state at each boundary, especially before side effects. An agent
cannot make a transition valid by announcing that the user approved it.

The human-reproduction checkpoint is a tool boundary backed by the real
WebUI confirmation record. It is not an agent called "Human". Approval applies
only to the matching finding and interruption; it does not approve all future
tools or all pending findings.

Use the SDK runner for model and handoff execution. Keep long-lived scheduling,
confirmation records, publication deduplication, and Docker lifecycle in the
application. SDK state, business records, and project files are separate
resources and need coordinated recovery.

## Workflow

1. Accept a repository URL and verify access with the configured identity.
2. Create a local checkout and record its revision.
3. Discover project commands for the supported language and attempt setup in
   the project's Docker environment.
4. If setup fails, display diagnostics and request WebUI configuration. Do not
   report the failure as a verified project bug.
5. Analyze the runnable project and record suspected findings and evidence.
6. Present each actionable finding in the WebUI as awaiting user reproduction.
7. The user personally reproduces the problem and records the result.
8. For a confirmed finding, check publication eligibility and duplicates,
   submit an issue, prepare a repair, and verify it.
9. Review the patch and submit a PR when the required checks pass.
10. Observe subsequent PR status. The project owner reviews and merges.

The confirmation of one finding must not unlock unrelated findings.
The coordinator must preserve pending findings through restarts and daily
runs without treating elapsed time or an agent message as confirmation.

## WebUI

- Repository list: URL, access status, environment status, last run, and schedule.
- Repository detail: detected commands, user overrides, runs, and configuration
  errors.
- Findings inbox: waiting for reproduction, confirmed, not reproduced, and
  dismissed states. The latter two state names are proposed UI behavior.
- Finding detail: revision, reproduction instructions, expected and observed
  behavior, relevant evidence, on-demand environment startup and status,
  environment access, and confirmation controls.
- Repair detail: patch, verification results, review findings, and GitHub links.
- Settings: provider configuration, execution limits, and schedule settings.

Pending reproduction reminders remain in the WebUI. Reading or dismissing a
notification is not equivalent to confirming reproduction. Deduplication
should prevent unchanged findings from producing a new reminder every day.

## On-Demand Reproduction And Feedback

Confirmed requirement: project containers must not stay running while waiting
for the user. The following interaction and persistence mechanics are proposed
to implement that requirement.

1. Preserve the finding, checkout revision, runtime configuration, diagnostics,
   and any explicitly retained reproduction data; stop the project environment
   when active work is complete and the finding is waiting for the user.
2. Keep the management WebUI and coordinator independent of that environment.
   Stopping a target project must not stop the feedback interface.
3. Offer an environment-start action on the finding detail page. Restore the
   pinned project environment and report readiness or an explicit startup error
   before directing the user to the project UI or reproduction commands.
4. The user personally reproduces the bug, then submits a reproduction result
   in the management WebUI. A successful result means the original bug was
   observed; it does not mean a repair has passed verification.
5. Persist the confirmation against the finding, revision, and environment
   evidence before acknowledging acceptance or scheduling further work.
6. The coordinator resumes the confirmed finding, performs publication checks,
   submits the issue, and dispatches repair and verification. The runner starts
   a project environment when those tasks need one.

Agent or project callbacks cannot satisfy the human checkpoint. A stopped
project container is not a reason to reject an otherwise valid user result.
Unsuccessful reproduction does not unlock issue submission or repair.

The proposed confirmation form presents the recorded reproduction instructions
and allows notes and attachments. Which fields are mandatory remains undecided.
Repeated submissions must not create duplicate issues or repair jobs. The UI
must not report success until the backend has durably accepted the result.

The management services still need the execution host to be available. If it
is offline, an unacknowledged click is not a saved confirmation. On restart,
the coordinator resumes only persisted work. Containers do not preserve live
process state merely by retaining files; required reproduction fixtures and
startup conditions need an explicit retention policy.

## Proposed Safety And Reliability Defaults

- Give project containers no access to model credentials, GitHub publishing
  credentials, or the Docker management socket.
- Restrict mounts, privileges, network access, and resource use through runner
  policy. Repository instructions cannot modify this policy.
- Treat repository files, issues, logs, and agent outputs as untrusted inputs;
  none of them can authorize publication or record human confirmation.
- Use isolated repair branches and do not overwrite existing user work.
- Bind confirmations to finding evidence and a repository revision. Define
  invalidation rules before automatically applying a fix to a newer revision.
- Preserve the pre-repair reproduction evidence and verify behavior after
  the patch; do not accept deleting or weakening tests as proof of a fix.
- Use durable publication identifiers and reconcile uncertain remote outcomes
  before retrying to avoid duplicate issues or PRs.
- Use bounded retries and execution times even with the currently unlimited
  model-spending configuration.
- Keep suspected confidential security findings out of automatic public
  publication until a separate disclosure policy is defined.

These defaults are proposed design constraints, not claims that isolation
or protection is already implemented.

## Remaining Product Decisions

- How long should stopped environments' files and reproduction data be retained?
- When should an environment started by the user stop after reproduction or
  inactivity, and what state must remain available?
- What evidence fields must accompany the user's confirmation?
- How should failed reproduction, dismissal, and later rediscovery interact?
- What local and future remote access protection should the WebUI use?
- What daily execution time and timezone should apply?

## Remaining Engineering Decisions

- Host readiness, Docker images, supported runtime versions, and service dependencies.
- Frontend and Python web-service frameworks, pinned Python and Agents SDK
  versions, persistence, and deployment choices.
- SDK interruption serialization, restoration of registered agents and tools,
  and cross-process resume tests.
- Evaluate the SDK's Docker sandbox integration against the selected runtime
  version, Windows host, preview ports, and environment-retention requirements.
- Credential setup, API permission scopes, and the detailed runner policy.
- Retry, timeout, concurrency, storage-retention, and cleanup limits.
- Verification gates and behavior when repository tests are unavailable.
- Revision-drift handling, deduplication, and migration of pending work.
