# ADR 0003: Automated Fork-Based Contributions

Date: 2026-10-09
Status: accepted; bounded by the human checkpoint in ADR 0007

## Context

Version one accepts accessible repositories by URL, including repositories
the user does not own. The user has explicitly permitted automatic forking
into the configured GitHub account and issue and PR submission as that account.

## Decision

The system may create a fork under the configured GitHub account without
per-action approval. When direct write access to the selected repository is
unavailable, it may prepare repair branches in that fork and propose changes
to the original repository through pull requests.

The system may submit issues and pull requests using the configured account.
For each finding, this requires prior human reproduction confirmation as
specified in ADR 0007. This authorization remains subject to available
permissions and repository restrictions. It does not authorize impersonating
a project owner, bypassing access controls, or modifying existing user work
in a fork.

The original project's owner reviews and performs the merge, as specified
in the amended ADR 0001. The system does not execute merges.

## Consequences

- Fork ownership is not approval authority for the original repository.
- The specific GitHub account and authentication mechanism remain undecided.
- Fork reuse, branch isolation, cleanup, and contribution-failure behavior
  need operational rules.
- Evidence thresholds, duplicate detection, and publication limits must be
  defined before unattended submissions are enabled.
- Fork-based contributions do not bypass human reproduction or transfer
  merge responsibility to the system.
