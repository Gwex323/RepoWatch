# ADR 0004: Reproducible-Bug Publication Scope

Date: 2026-10-09
Status: amended to require human reproduction under ADR 0007

## Context

The user accepted a version-one scope that limits automatic submissions to
reproducible bugs, including in repositories owned by other people. The latest
clarification requires human reproduction before the system takes the next
step for a discovered problem.

## Decision

A human must reproduce and confirm a suspected bug before the system
automatically publishes an issue or starts repair work for that finding.
An agent's successful reproduction or test output alone is insufficient.

The system's single user performs the reproduction personally and records
the result in the WebUI, as confirmed under ADR 0007.

Refactoring suggestions, code-style findings, and unverified suspected
problems remain in the analysis report. They do not independently qualify
for automatic issue or repair-PR submission.

Reproduction evidence is necessary, not sufficient, for publication. Other
gates, including duplicate checks and repair verification, remain to be
specified.

## Consequences

- A suspected problem and a bug eligible for publication are distinct states.
- The system must retain evidence supporting publication decisions.
- Scheduled checks, retries, and waiting time must not turn an unconfirmed
  finding into a human-confirmed bug.
- Failing to reproduce a finding does not establish that the code is correct;
  it means the finding remains ineligible for automatic publication.
- A verified original bug does not by itself establish that a proposed fix
  is correct.

## Open Questions

- Which evidence formats and repeatability checks must accompany the user's
  WebUI reproduction confirmation?
- How are pre-existing failures, environment failures, and flaky behavior
  distinguished from a newly identified bug?
- What verifies that a patch fixes the problem without regressions?
- How are findings requiring confidential disclosure handled before any
  public submission?
- Where can the user inspect the analysis report and supporting evidence?
