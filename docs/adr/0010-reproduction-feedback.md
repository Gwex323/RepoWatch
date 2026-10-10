# ADR 0010: Stopped Waiting Environments And WebUI Feedback

Date: 2026-10-09
Status: accepted waiting policy; proposed restart and feedback mechanics

## Context

The user does not want project containers to keep running while waiting for
manual reproduction and asks how the system receives feedback afterward.
Prior decisions require the user to reproduce the bug personally and confirm
the result in the WebUI.

## Confirmed Requirements

- Project containers do not stay running solely to wait for user reproduction.
- The user submits reproduction results through the management WebUI.
- A confirmed reproduction unlocks subsequent issue submission and repair,
  subject to the other gates. The project owner still performs the merge.

## Proposed Design

Separate the management WebUI and coordinator from the target project's
container lifecycle. Keep findings and confirmations in durable management
storage, not solely inside a disposable project environment.

Provide a per-finding action to start the recorded project environment on
demand. Expose readiness, project access, and reproduction instructions.
The user then reproduces the problem and submits a result in the WebUI.

The backend persists a successful confirmation and then schedules eligible
follow-up work. It must not wait for a callback from the project container.
Start or reuse an appropriate environment when repair and testing need it.

ADR 0011 selects OpenAI Agents SDK. The proposed feedback implementation now
maps the WebUI confirmation to a specific saved SDK approval interruption.
Application validation remains authoritative before approval and resumption;
SDK state does not replace the durable human confirmation record.

## Proposed Reliability Rules

- Bind the result to a specific finding and recorded repository revision.
- Preserve relevant configuration and reproduction data across container stops.
- Do not assume process memory or external service state survives a restart.
- Startup failure and unsuccessful reproduction do not unlock publication.
- Acknowledge a confirmation only after it has been durably recorded.
- Make repeated submissions and dispatch retries safe against duplicate work.
- Recover persisted pending work after the management service restarts.
- Do not claim feedback can be received while the host or management service
  is unavailable.

## Open Questions

- What evidence fields are mandatory in a reproduction result?
- How should manually started environments stop after use or inactivity?
- Which files, fixtures, and service data must be retained, and for how long?
- What repository or configuration changes invalidate prior confirmation?
