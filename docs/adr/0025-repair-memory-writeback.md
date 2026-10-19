# ADR 0025: Repair experience write-back to shared memory

Date: 2026-10-19 (Asia/Shanghai)
Status: implemented

## Problem

The memory service already had a `fix` category, source-key deduplication,
role-scoped retrieval and audit history, but real-repository repairs never
wrote memories. `capture_fix` only covered the controlled fixture `runs` path.
The same repository therefore lost the "how this was fixed" experience after a
published repair, and README recorded that repair experience was not written
back automatically.

## Decision

When an owner marks a finding resolved through the merged-PR tier of ADR 0024,
the same disposition transaction writes one bounded `fix` memory. The owner
assertion tier writes nothing: without a published patch there is no repair
content worth extracting, and an unfounded memory would pollute later analyses.

The memory is workflow-sourced and evidence-bound:

- `source_key = repair:{repair_id}`; the existing
  `UNIQUE(repo_id, source_key)` plus `INSERT OR IGNORE` make writes idempotent.
  Retries or repeated capture attempts never create a second entry.
- Content is extracted from the stored finding, patch, verification and review
  records: problem title and explanation, patch summary, edited paths,
  regression test paths, verification outcome and reviewer summary. Every
  field is length-bounded; evidence records the repair, finding and analysis
  IDs, the merged PR URL, exit-code summaries and reviewer risks.
- The memory binds to the repair's repository and exact revision, matching the
  existing retrieval policy. Future analyses of the same revision include it
  automatically through the existing bounded context loader.
- The entry states explicitly that it is reference experience and does not
  prove the current revision still applies. Cross-version reuse is not enabled
  in this milestone; ADR 0021 historical rechecks remain the cross-version
  mechanism and still require current-source evidence.

Memory stays reference data, never authorization: a fix memory cannot confirm
a finding, queue a repair, or change any human decision. Models receive it as
untrusted context only.

## Verification

Regression coverage includes merged-PR write-back with content and evidence
assertions, idempotent re-capture, no write on owner assertion or
not-reproduced dispositions, and an end-to-end check that a later analysis of
the same repository and revision receives the fix memory in its context.
