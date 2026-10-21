# ADR 0005: Scheduled Repository Checks

Date: 2026-10-09
Status: implemented for repository-level daily checks

## Context

The user wants the system to support scheduled repository checks after
the initial analysis.

## Decision

The system supports recurring scheduled checks for each registered GitHub
repository. The owner can enable or pause the schedule and select a local
`HH:MM` check time.

The scheduler runs at most once per repository per local calendar day. Before
creating work, it claims the day atomically and checks for an active execution
pipeline. If one exists, the day is recorded as `skipped_active` and no new
environment is created. Failures retain a stable code and do not silently
disappear.

Daily checks reuse the existing download → execution → analysis → Issue intake
pipeline and add no new repair, publication, or merge authority.

Scheduled runs remain subject to the existing repository-access,
publication-evidence, and owner-approval requirements.

The user subsequently selected WebUI-only reminders for findings requiring
personal reproduction. No email or external messaging integration is needed
for those reminders.

## Consequences

- Scheduling is durable in SQLite and survives service restarts.
- A repository remains independently pausable.
- The first version uses the host local timezone; it does not implement cron
  expressions or cross-host coordination.

## Open Questions

- Should unchanged repositories skip a full re-analysis after a successful run?
- Should failed daily checks retry later on the same day?
- Should the UI expose historical daily-check outcomes beyond the latest record?
