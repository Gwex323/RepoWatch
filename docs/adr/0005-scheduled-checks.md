# ADR 0005: Scheduled Repository Checks

Date: 2026-10-09
Status: accepted for daily scheduling; execution policy unresolved

## Context

The user wants the system to support scheduled repository checks after
the initial analysis.

## Decision

The system will support recurring scheduled checks of repositories selected
through the URL-based intake flow.

The default check frequency is once per day, as confirmed by the user.
No particular execution time or timezone has been selected.

Scheduled runs remain subject to the existing repository-access,
publication-evidence, and owner-approval requirements.

The user subsequently selected WebUI-only reminders for findings requiring
personal reproduction. No email or external messaging integration is needed
for those reminders.

## Consequences

- Scheduling is part of the version-one requirements.
- The default frequency is daily; the execution time, timezone, and
  per-repository configuration policy remain undecided.
- This decision describes the system to build; it does not create a live
  automation in the current chat.

## Open Questions

- At what time and in which timezone should daily checks run?
- Can each repository override the default schedule?
- What happens when a previous run is still active at the next scheduled time?
- Should an unchanged repository receive another full analysis?
- How do pause requests, retry limits, and spending limits affect scheduled runs?
- How should WebUI reminders be deduplicated across scheduled runs?
