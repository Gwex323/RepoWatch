# ADR 0008: Automatic Project Setup With WebUI Fallback

Date: 2026-10-09
Status: accepted workflow; execution and configuration details unresolved

## Context

Version one targets Python, Go, and TypeScript repositories. Specific initial
repositories have not been selected. The system must download and run projects
locally so the user can personally reproduce suspected bugs.

## Decision

The system first attempts to discover the project's dependency installation,
startup, and test commands, then attempts to execute them under the execution
policy.

If automatic setup or execution fails, the user can provide or correct project
configuration through the WebUI. Failure must remain visible rather than
being reported as successful execution or a passing test.

ADR 0009 subsequently selects local Docker containers for these commands.
This does not authorize unrestricted execution on the host. Detailed
execution policy must be defined before unattended repository commands
are enabled.

## Design Implications

- Project-level command configuration is distinct from global system settings.
- Setup status must distinguish a runnable project from one needing user
  configuration.
- Environment and startup failures are not automatically verified project
  bugs. Suspected bugs still require human reproduction under ADR 0007.
- Configuration errors must not cause unbounded automatic retries.
- The WebUI needs enough command and diagnostic information for the user to
  correct configuration, without exposing credentials.

## Open Questions

- Which repository metadata and documentation should command discovery use?
- How should command arguments, working directories, readiness checks, and
  long-running processes be represented and validated?
- Which overrides persist across runs, and what repository changes require
  them to be revalidated?
- How should secrets and external service dependencies be configured?
- Which detailed restrictions govern dependency installation and execution
  inside the Docker environment selected in ADR 0009?
- How should setup retries be triggered after a user changes configuration?
