# ADR 0001: Automation Boundary and Owner-Executed Merge

Date: 2026-10-09
Status: amended by the latest user clarification and ADR 0007

## Context

The user initially requested analysis, issue submission, bug repair, and
PR submission without per-action approval, with owner approval before merge.
The user subsequently required human reproduction before a finding proceeds
and specified that the project owner, not the system, performs the merge.
The following decision reflects those latest requirements.

## Decision

Within an authorized repository scope, the system may analyze code and
discover suspected bugs automatically. A human must reproduce and confirm
each suspected bug before its issue submission or repair proceeds.

After that checkpoint, the system may open an issue, prepare and verify a
fix, push a repair branch, and create or update a PR without separate
per-action approvals, subject to other quality and permission gates.

The project owner reviews and executes the merge. The system must not merge
a PR or enable automatic merging on the owner's behalf. Passing tests or
receiving agent approval does not replace either human checkpoint.

## Consequences

- Automated preparation is bounded by mandatory human reproduction.
- Merge execution is outside the system's responsibilities.
- This decision does not authorize direct changes to a target branch as an
  alternative to the pull-request approval process.
- The system must retain human reproduction confirmation and its supporting
  evidence. Revision binding and invalidation rules remain to be specified.
- The system may observe PR review and merge status without executing merges.
- No target repository, credentials, or implementation has been selected.
