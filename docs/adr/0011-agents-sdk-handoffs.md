# ADR 0011: OpenAI Agents SDK And Native Handoffs

Date: 2026-10-09
Status: Python SDK selected; agent graph and integration proposed

## Context

The user explicitly selected the OpenAI Agents SDK architecture, including
handoffs. Earlier drafts left the agent runtime unspecified.

## Decision

Use OpenAI Agents SDK as the agent runtime and use its native handoff
mechanism for responsibility transfers between specialists. Do not replace
that mechanism with a custom model-call loop.

Keep the already confirmed requirements: Docker project execution, daily
checks, human reproduction confirmed in the WebUI, stopped waiting project
containers, and project-owner-only merging.

The user subsequently confirmed Python as the agent backend language and
selected the Python edition of OpenAI Agents SDK. Exact Python and SDK
package versions remain to be pinned after compatibility checks.

## Official Capability Basis

- Handoffs transfer ownership to a specialist; agents-as-tools keep ownership
  with the caller. Use handoffs for the selected specialist chain. [S1]
- The application owns deployment, storage, tools, and approval decisions;
  the SDK owns the agent execution loop. [S2]
- Tool approval can interrupt a run and return resumable state. The application
  can store that state and later approve or reject the matching interruption
  before resuming. [S3, S4]
- The sandbox documentation includes Docker integration and distinguishes
  runner state, sandbox session state, and saved workspace contents. Host
  compatibility and lifecycle behavior still need verification. [S5]

## Proposed Human-Reproduction Integration

1. Persist the finding and reproduction evidence before reaching the gated
   continuation tool; the UI must not depend on that tool executing to obtain
   the evidence it needs to request confirmation.
2. Mark the continuation tool as requiring human approval. Persist the SDK
   interruption and serialized state with the finding and revision identifiers.
3. Stop the project environment while waiting, without holding an active
   agent loop open or granting default approval.
4. The user starts a reproduction environment on demand and personally
   verifies the bug.
5. Receive confirmation through the WebUI. Validate the user, finding, pinned
   revision, pending interruption, and any invalidation conditions.
6. Durably record the result and enqueue one resumable job. Approval is scoped
   to that interruption, never a blanket approval of all pending tool calls.
7. Restore the matching SDK state and registered agent/tool configuration,
   apply the recorded decision, and resume the run.
8. Issue submission, repair, review, and PR submission still enforce their
   own side-effect checks. No agent or publisher receives merge capability.

SDK approval represents the user's completed reproduction, not merely
permission for an agent to attempt reproduction. A user reporting failure
does not authorize the continuation tool.

## Proposed Handoff Contracts

- Keep repository scanning separate from per-finding work so one manual wait
  does not pause all repository discovery.
- Allowlist specialist transitions; carry finding IDs, revision IDs, evidence
  references, and task scope rather than unconstrained authority.
- Verify referenced records in trusted application storage. Model-supplied
  handoff metadata is not proof of human approval.
- Limit reviewer-to-fixer loops and total SDK turns; exhaustion leaves a
  visible blocked or failed state.
- Use tool-level policy checks for effects. Input and output guardrails alone
  are not the full permission system.
- Separate the LLM PR author from the trusted GitHub tool implementation.
- Do not interpret a handoff as a queued job, parallel worker, human approval,
  or transfer of operating-system credentials.

## Proposed State And Recovery

Maintain three related but independent records: business workflow state,
serialized SDK run state, and the reproduction/repair environment manifest
with its retained files and data.

Choose one SDK history-continuation strategy per run. Pin the SDK and agent
definition versions for persisted interruptions, or explicitly migrate them
before resumption. Do not assume an arbitrary future SDK version can resume
every previously saved run.

The application must recover safely if it crashes between confirmation,
dispatch, SDK resumption, and publication. Validate resume tokens and avoid
duplicate side effects. Never deserialize untrusted user-supplied runtime
state or keep live database clients and credentials in an exported snapshot.

## Acceptance Checks Before Implementation Is Considered Complete

- A real SDK handoff changes the active specialist and preserves task scope.
- An unconfirmed finding cannot publish an issue, start repair, or create a PR,
  including through a direct tool call or an alternative handoff route.
- A waiting run can be saved, the worker restarted, and the same finding
  resumed after its authenticated WebUI confirmation.
- Confirming one finding leaves every other finding blocked.
- A negative result, stale revision, or unrelated interruption cannot unlock
  the workflow.
- Repeated confirmation and worker retries cannot duplicate remote submissions.
- Project containers need not stay alive to preserve pending confirmation.
- No registered tool or handoff path permits merging or enabling auto-merge.

## Open Implementation Choices

- Pinned Python runtime and Python SDK package versions.
- Provider adapters and tool-calling compatibility for selected models.
- Durable state storage, task dispatch, and serialization format.
- Docker sandbox integration on the Windows host and future Linux deployment.
- Agent-specific tools, structured outputs, history filtering, and model choice.
- Trace export, redaction, access control, and retention for private repository
  data and user reproduction evidence.

## Sources

Official OpenAI documentation consulted on 2026-10-09:

- S1: Orchestration and handoffs:
  `https://developers.openai.com/api/docs/guides/agents/orchestration`
- S2: Agents SDK:
  `https://developers.openai.com/api/docs/guides/agents/sdk`
- S3: Guardrails and human review:
  `https://developers.openai.com/api/docs/guides/agents/guardrails-approvals`
- S4: Results and state:
  `https://developers.openai.com/api/docs/guides/agents/results`
- S5: Sandbox agents:
  `https://developers.openai.com/api/docs/guides/agents/sandboxes`
