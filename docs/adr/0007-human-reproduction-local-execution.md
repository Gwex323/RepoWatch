# ADR 0007: Local Execution and Mandatory Human Reproduction

Date: 2026-10-09
Status: accepted requirements; interaction and isolation details unresolved

## Context

The user clarified three requirements: the project owner performs merges;
the system downloads and runs repositories locally; and discovered problems
must be reproduced by a human before proceeding.

This narrows the earlier blanket preference for automation without approvals.
It does not require human approval for every individual action after a
finding has passed the reproduction checkpoint.

## Decision

The system checks out repositories into local workspaces on its execution
host and runs them as part of analysis. The initial host is Windows, with
Linux deployment planned later.

For each suspected bug, the system pauses progression after discovery until
a human has reproduced and confirmed it. Issue submission, repair work, and
repair-PR submission for that finding remain blocked before confirmation.
Agents may collect evidence and assist reproduction but cannot act as the
confirming human.

The user confirmed that they personally operate the local project to
reproduce the problem, then record the result in the WebUI. This is the
responsibility of the system's single user, not the project's owner unless
they happen to be the same person.

After confirmation, automated issue submission, repair, verification, and
PR submission can proceed within the previously defined scope and gates.
The project owner reviews and performs the final merge. The system does
not merge or enable auto-merge on the owner's behalf.

## Consequences

- A local clone alone does not satisfy the requirement to run the project.
- Unsupported environments or startup failures must be reported honestly;
  the system must not claim successful execution or reproduction.
- The human checkpoint requires persistent state across daily runs and
  restarts. A schedule, retry, or timeout cannot substitute for confirmation.
- Under ADR 0010, project containers do not stay running during the wait.
  The user's WebUI confirmation must not depend on a project container
  sending a callback.
- Human reproduction of the original bug does not replace verification
  of a proposed repair.
- Local execution does not authorize unrestricted host execution. Dependency
  installation, repository scripts, credentials, and network access still
  require an isolation design. ADR 0009 selects local Docker containers;
  detailed restrictions remain to be specified.

## Open Questions

- What evidence must accompany the user's reproduction result in the WebUI?
- How is a finding tied to a commit and reproduction environment?
- Which changes invalidate an existing confirmation?
- How are rejected, unrepeatable, and still-waiting findings handled?
- How are runnable environments exposed to the human without unsafe access
  to the host or to credentials?
- What project state should be retained, and for how long, to support
  restarting reproduction environments on demand?
