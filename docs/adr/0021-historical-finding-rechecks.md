# ADR 0021: Evidence-bound historical finding rechecks

Date: 2026-10-16
Status: implemented and locally validated; no live model rerun

## Problem

An empty new-finding list did not explain whether previously reported problems had
been revisited. Exact-revision memories were not a substitute for unresolved
cross-revision findings. In particular, the legacy JWT whitespace example described
correct rejection of invalid input, yet labeled it a validation bypass. Neither
that historical assertion nor a clean new report establishes current truth.

## Contract

- Before the first model call, freeze completed same-repository findings whose human
  status is waiting_reproduction or confirmed. Include origin finding ID/version,
  analysis ID/revision, human status, original claim/reproduction/source evidence.
  Human not_reproduced records and other repositories are excluded. A prior model
  conclusion never removes a human-confirmed claim from this candidate set.
- At most eight items: confirmed first, then oldest analyses and stable finding ID.
  Counts disclose all excluded-by-cap items as not covered. Descriptions, steps and
  historical quotes have explicit length limits and a clipping flag. Credentials
  are redacted with the existing analysis policy. No feedback note or credential
  store content is added. This is additional context to the already configured
  provider, not permission to make a paid validation call during development.
- Store this input in the analysis context under the running version guard. Count,
  selection and context save share one transaction. Retries reuse it, including its
  human-status snapshot, rather than silently changing what the model reviewed.
- Separate historical_rechecks from new findings. Allowed outcomes: still_present,
  fixed, suspected_false_positive, not_covered. Unknown/duplicate IDs are rejected.
  Non-coverage may have no evidence. All other outcomes require current source
  quotes, bounded line ranges, and actual SDK-tool reads. Supplied evidence for
  non-coverage is still validated. Line-number normalization uses the same gate.
- The reviewer must compare old reproduction logic against current behavior and
  challenge contradictory claims. A merged PR, absent path, passing tests, error
  wording or an empty new-finding list alone cannot demonstrate a fix or a bypass.
  This is source review, not a new execution or formal semantic proof.
- Missing decisions become explicit not_covered records after report validation,
  including engines/legacy fixtures that omit the new optional field. Old stored
  reports without a historical input snapshot remain unchanged, with no invented
  retroactive review. Bounded format/evidence correction retains the origin packet.
- Persist outcomes in the existing report JSON under existing cancellation/config
  identity/CAS gates. No schema migration or new action endpoint. The API joins
  frozen origins to outcomes for UI display, never updates original findings,
  feedback, repairs, issues or PRs. Model claims do not authorize any write action.

## UI

A separate read-only historical section shows origin revision, finding ID/version,
original human status, clipping, outcome, rationale and expandable current source
quotes. Cap omissions are explicitly disclosed. Human reproduction controls remain
only on new findings. No new findings explicitly does not mean all old issues are
resolved. Narrow displays wrap long IDs, paths, titles and source quotes.

## Validation

Deterministic local tests cover four outcomes, SDK handoffs, conservative omissions,
legacy reports, unknown/duplicate IDs, forged/unread/stale quotes, normalization,
repo/revision isolation, clipping/redaction, cap counts, persistence/restart/retry,
unchanged human feedback, no repair tasks, and cancellation/stale late completions.
Browser tests cover all four labels, old confirmation retention, cap omissions,
current evidence and absence of action buttons on desktop/mobile.

No real model call or GitHub publication is part of this change. Existing historical
findings and completed analyses are not rewritten; the next newly run analysis is
needed to produce real rechecks of the user's repository.

### Local verification results

- Historical recheck tests: 29 passed; combined with repository-analysis tests: 145 passed.
- Full backend suite: 564 passed, ten optional Docker tests skipped (351.61 seconds).
- All eleven browser tests passed against the isolated QA database on port 8791.
  Desktop/mobile screenshots were captured; the mobile historical section was
  visually inspected, with no horizontal overflow.
- TypeScript/Vite build, Ruff and whitespace checks passed.
- The QA process was identity-checked and stopped after verification. The personal
  backend was not restarted, no personal analysis was queued, and no source was
  sent to a live model. A backend restart and a newly run analysis are needed to
  generate actual historical recheck opinions; this work does not invent them.


## Follow-up: required evidence reads before handoffs

A live attempt (analysis_e5aadbcb99144fc1) had all three native handoffs and two
source-read events, but no execution_records evidence event. After a bounded quote
correction, final validation rejected the missing execution read. The old generic
review-incomplete message incorrectly suggested source or handoff failure as well.
No report or findings were saved. This was not a GitHub or Docker permission error.

Root cause (implicit assumption / test coverage gap): prompts requested execution
reads and all deterministic successful models complied. The runtime checked the
requirement only at the end; historical-input tests did not include an agent that
read source and completed handoffs while skipping the execution tool. More tests
of already-compliant model output did not make that prerequisite unavoidable.

Prevention: the first analyst now has two bounded mandatory SDK-tool requests:
read_execution_records, then a successful read_source_file. Each request exposes
only that read tool, disables parallel calls and removes handoffs until it runs.
The wrapper validates the actual provider response before the SDK executes it;
wrong/mixed calls fail explicitly. No synthetic calls, events, flags or handoffs
are created. Invalid source paths cannot escape existing stage/time/read budgets.
Execution reads include the saved access_result as well as config/jobs/test_result.
All three later native handoffs and final evidence gates remain mandatory.

Distinct errors identify absent execution reads, absent source reads, incomplete
handoffs, and providers ignoring a named required read. A new required_read event
records only the requested prerequisite; the existing evidence/source events still
mean actual handler execution. UI labels distinguish these cases. Legacy errors
and failed attempts are not retroactively rewritten or automatically retried.

Regression coverage reproduces source-only/early handoff behavior, checks actual
read-before-handoff ordering, bounded invalid paths, exhausted time, mixed calls,
provider noncompliance, error persistence and absence of saved findings. Provider
adapter fixtures also verify named tool_choice with exactly one exposed tool.
Desktop/mobile browser tests verify the precise error and both prerequisite labels.
No real provider call is part of development validation.


Follow-up validation: all ten new prerequisite regressions passed; the combined
analysis suites passed 155 tests. Full backend regression passed 574 tests with ten
opt-in Docker tests skipped (392.39 seconds). All eleven browser tests passed on
isolated QA port 8792. TypeScript/Vite, Ruff and whitespace checks passed. The QA
instance was stopped and its port was verified closed.

After an idle check and verified SQLite/log backup
(data/backups/restart-required-reads-20261016-144541), the personal workbench was
restarted at the original port 8765. HTTP and updated static resources were checked.
Database integrity was OK; task IDs/states/versions, environment states/versions,
model-call count and finding/event counts matched the pre-restart baseline. Project
preview state was retained. No paid analysis retry, project test, repair or GitHub
publication was initiated. The old failed record remains for an explicit owner retry.


## Follow-up: bounded pure source batches and shared analysis error messages

Two owner-triggered retry requests returned HTTP 200 but subsequently failed at
analysis_source_read_not_followed. Execution-record reads succeeded; source reads
were rejected before the SDK executed them. The pipeline UI did not recognize the
analysis error code and replaced it with a generic refresh/retry message. The raw
provider responses were not retained, so the previous failures cannot be definitively
attributed to multiple matching calls, wrong calls, or absent calls.

The mandatory-source wrapper previously required exactly one function call. This
was unnecessarily narrower than ordinary bounded source exploration: compatible
providers can return multiple pure source reads despite parallel_tool_calls=false.
The wrapper now accepts one through the remaining per-stage read allowance, only
when EVERY call is the required read_source_file function. The original handler
still applies source-index, line, read/character, and deadline/stage budgets. Empty,
oversized, unknown-tool or mixed-handoff batches are rejected BEFORE execution.
Execution-record prerequisites still accept exactly one call. No missing read is
marked complete, and no evidence or handoff validation is bypassed.

Every mandatory response records required_read_validation containing fixed labels
and numeric counts (calls, matching, other, allowed, messages), never names of
unexpected tools, arguments, source, raw text or reasoning. These counts describe
request validation, not handler success; source/evidence events remain authoritative.
The pipeline and analysis panels share one analysis-error dictionary so future
analysis errors are not mistaken for failed configuration/refresh operations.
The source-read error now says request validation failed, not that the model never
requested a read. Historical errors and attempts are not rewritten.

Validation: 212 focused backend tests passed across prerequisites, analysis, historical
review, execution and pipelines; all twelve browser tests passed. New tests cover
1/2/10 actual source calls, budget-consuming invalid batches, oversized/empty/mixed
requests, one-call execution prerequisites, and credential-free diagnostics. UI
regressions cover detailed analysis failures in the pipeline, preserved passing
tests, and safe counter display. TypeScript/Vite, Ruff and whitespace checks passed.
No full-backend-suite rerun or live-model acceptance is claimed for this follow-up.

After verified idle backup at data/backups/restart-source-batches-20261016-152850,
the personal workbench was restarted on port 8765. All 27 table hashes matched the
backup afterward; integrity/FK checks passed and updated assets returned HTTP 200.
No install, project test, paid model retry, repair or GitHub write was triggered.
The QA instance on port 8795 was stopped. Actual provider completion remains for an
explicit owner analysis retry; new safe counters will distinguish any further failure.


## Follow-up: resume premature non-reviewer answers inside the same attempt

The next actual attempt passed mandatory execution/source reads (including two
source calls) and repository_analyst -> problem_investigator, then the investigator
returned text instead of its native handoff. reproduction_assistant and the
independent_reviewer never ran; the final-stage guard correctly refused a report.
This is workflow noncompletion, not an installation failure or a confirmed defect.

Root causes: shared instructions asked every role to return historical-review
entries, while only the final reviewer can submit a report. Prompts and final
validation alone were not enough to ensure intermediate roles actually handed off.
The budget wrapper forced closing only at budget exhaustion; an early final answer
could bypass that closing opportunity. Tests covered compliant handoffs or total
noncompliance, but not an isolated early answer recoverable without replaying work.

The final-output instruction is now reviewer-only. Other roles collect observations
and must use their native handoff. If the SDK still returns a non-reviewer final
answer, retain its to_input_list conversation (including observations and actual tool
results), close exploration only for that named role, and resume with that same agent.
Its next request exposes no read tools and explicitly selects the real native handoff.
Later roles retain their own budgets. The SDK executes and audits the actual handoff;
there is no synthesized transition or direct jump to the reviewer.

At most one such recovery per nonfinal role is allowed, using the existing closing
allowance. Role budgets/read counters/start times, shared timeout/output retry limits
and deadline remain unchanged. SDK max_turns is recalculated from the overall
remaining actual turns rather than reset for each continuation. An ignored forced
handoff fails immediately under the existing guard. Explicit refusals are respected,
not recovered. Old failed attempts are not retroactively replayed or declared fixed.

The final reviewer, exact three-handoff sequence, actual read/quote gates, config/
pipeline identity and cancellation CAS checks remain mandatory. Early text stays
untrusted observations, never findings, owner feedback or repair authorization.
Only a fully validated final review is committed. A handoff_recovery event signals
an attempted continuation; actual handoff events remain the completion evidence.
The UI distinguishes same-attempt handoff continuation from transport request retries.

Regression coverage: early completion at each of the three intermediate roles and
all three in one run; exactly one execution/source read, preserved tool outputs and
observations, real three-stage handoffs, bounded role/overall turns, refusal handling,
ignored closing, forged final quotes, durable completion and cancellation before a
late result. No project tests, commands or live model calls are replayed in these tests.


Final continuation validation: 600 backend tests passed, ten optional Docker tests
skipped (397.68 seconds). All twelve browser tests passed; the two analysis browser
cases were rerun after the final UI update to exercise the live same-attempt
continuation status rather than transport-retry wording. All ten recovery regressions
were rerun after role-scoping the continuation prompt. TypeScript/Vite, Ruff and
whitespace checks passed.

The personal backend was idle and backed up with logs at
 data/backups/restart-handoff-continuation-20261016-155400, then identity-checked and
restarted on port 8765. Updated assets returned HTTP 200. All 27 table hashes matched
the pre-restart backup; integrity and FK checks passed, and model-call/task state
history was unchanged. The isolated QA server on port 8796 was stopped. No personal
analysis, project test, reinstall, repair or GitHub operation was initiated. The old
failed attempt remains unchanged; actual model acceptance of the continuation path
is not claimed without a new explicit owner-triggered analysis.
