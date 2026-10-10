# ADR 0002: URL-Based Repository Intake

Date: 2026-10-09
Status: accepted; automatic fork authorization recorded in ADR 0003

## Context

The user wants version one to accept any accessible GitHub repository by URL,
rather than requiring the repository to belong to the user.

## Decision

A repository URL is the per-repository input for starting analysis. A
repository need not be owned by the user to be eligible.

Accessibility means the system can read the repository using its configured
identity. A repository being visible in a user's browser does not establish
that the system has the same access. Account-level authentication setup is
not yet specified.

Read access and contribution permissions are separate checks. URL intake
does not authorize bypassing access controls or repository contribution
restrictions.

## Consequences

- Intake must validate the URL and check repository access before analysis.
- An inaccessible repository must produce an explicit access failure.
- The contribution route must be determined independently of read access.
- Automatic fork creation into the configured GitHub account is authorized
  by ADR 0003. The specific account and behavior when a requested contribution
  is unavailable remain undecided.
- The project-owner approval requirement from ADR 0001 still applies.
