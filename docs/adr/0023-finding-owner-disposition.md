# ADR 0023: Owner disposition for historical findings

Date: 2026-10-19 (Asia/Shanghai)
Status: implemented

## Problem

ADR 0021 made the historical recheck section read-only: model recheck outcomes
never mutate original records. This correctly prevents model claims from
overriding human decisions, but it also removed every path for the owner to
record a disposition on an old finding. A superseded analysis can never regain
feedback controls, so findings that are no longer re-reported as new findings
stay in `waiting_reproduction` or `confirmed` forever, keep occupying bounded
recheck candidate slots each round, and a confirmed false positive cannot be
retracted before it drives further repair attempts.

## Decision

Owners may close old findings through an explicit disposition, separate from
the existing reproduction feedback. `POST /api/analyses/{analysis_id}/findings/
{finding_id}/disposition` accepts only `decision=not_reproduced` with an
optional note. It applies to findings in `waiting_reproduction` or `confirmed`
status and intentionally does not require the origin analysis to be current;
stale analyses are exactly where the gap existed. The finding version is the
CAS guard; a mismatch fails with `finding_changed`.

The written feedback record keeps `actor=owner`, the analysis revision, the
timestamp, the note, `previous_status`, and a `disposition` flag. Closed
findings (`not_reproduced`) leave the historical candidate set through the
existing status filter; no snapshot or stored report is rewritten.

Repair interaction:

- A queued repair task for the finding is cancelled in the same transaction
  with a `cancelled` / `finding_disposition` event.
- A running repair blocks disposition with `repair_running`.
- Terminal repair records (failed, interrupted, cancelled, `pr_created`) are
  preserved as history and never deleted.

Model recheck outcomes still never change anything. Only the owner's explicit
disposition mutates a finding, and only toward `not_reproduced`; there is no
way to convert a retracted confirmation back into a confirmation without a new
reproduction on a current analysis.

## UI

Historical items now show the live finding status when it differs from the
frozen origin status, and expose one disposition button for findings still in
`waiting_reproduction` or `confirmed`. The frozen snapshot text remains
unchanged; live status is joined at read time. New findings keep their existing
reproduction controls.

## Verification

Regression coverage includes disposition of stale waiting and confirmed
findings, version CAS conflicts, closed-finding conflicts, queued-repair
cancellation, running-repair blocking, live status exposure in analysis
details, owner/CSRF enforcement, forged-field rejection, and exclusion from the
next historical snapshot. Browser tests cover the disposition control, the
current-status line, and the closed state.
