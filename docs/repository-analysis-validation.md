# Repository Analysis Validation

Date: 2026-10-13
Branch: feature/repository-execution

## Final Report Format Follow-Up (2026-10-14)

The latest local failure completed all native handoffs and failed in
independent_reviewer after 135 seconds. The provider ended with stop, 31 output
tokens and no truncation. Historical analysis_json_invalid conflates syntax
and schema failures, and the original payload is not retained; no exact
missing field or malformed response can be established retrospectively.

The final reviewer now receives its complete strict schema in the prompt.
Invalid final syntax or fields permit one format correction within the same
conversation, with tools removed, no replay of earlier stages, and the existing
deadline. Invalid output is neither accepted, modified locally nor persisted.
The exact glm-4.7 on official BigModel/Z.AI Chat Completions endpoints uses
JSON Object on the wire while strict SDK output validation remains in place.
Other provider/model contracts are unchanged. Output protocol, safe validation
category and the correction event are visible in the UI.

Validation: 174 backend tests passed, 3 optional Docker cases skipped; all
8 browser tests passed. Mock HTTP tests exercise actual SDK adapters, tools,
native handoffs, the JSON Object request and strict rejection/correction of
missing fields. Tests also cover malformed JSON, extra fields, bounded
correction, unchanged evidence reads, forged evidence rejection, blocked tools
during correction, correction after stage budget exhaustion and the original
deadline. Status resolution preserves stored model configuration and encrypted
credentials. Ruff, TypeScript, production build and whitespace checks passed.
Desktop settings and 390-pixel mobile format-error screenshots were inspected.

No live model call was initiated for this fix. Live provider compatibility and
review quality require an explicit retry; historical failures are retained.

## Reasoning Deadline Follow-Up (2026-10-14)

The latest live attempt completed all handoffs and reached independent_reviewer
at 228 seconds. Its closing response ended with length at 437 seconds:
5,000 output tokens, including 4,999 reasoning tokens. Output expansion then
hit the per-request timeout and the following request was cancelled at the
600-second overall deadline. No findings were saved.

Changes request low reasoning effort for the exact glm-5.3 model in auto mode,
while keeping provider defaults for other models. Owner settings allow an
explicit override or restoration of provider defaults. Role time windows and
a 240-second finalization threshold close exploration at request boundaries;
these are not a guarantee that a slow provider will finish.

Validation: 158 backend tests passed, 3 optional Docker cases skipped;
8 browser tests passed. The mock HTTP provider verifies that the actual
Chat Completions request includes reasoning_effort=low. Tests cover default
resolution, encrypted-key preservation, explicit default/effort settings,
stage-time and overall-time closure, and expired requests without provider
calls. Ruff, TypeScript, production build and whitespace checks passed.

The owner workbench was restarted at http://127.0.0.1:8765 using the original
database. No live retry was triggered in this session. Existing historical
failure records remain unchanged. Gateway support for reasoning_effort and
live completion under the revised budgets are not yet verified.
During implementation the owner changed the configured model to glm-4.7.
That choice was preserved; auto mode resolves to provider-default reasoning
for this model, not the glm-5.3-specific low setting. General time budgets
apply to both models.

## Tool-History Finalization Follow-Up (2026-10-14)

The next owner-triggered attempt failed after 354 seconds despite the prior
JSON-mode and format-correction changes. Both final responses were normal-stop,
32-token responses classified as invalid JSON. Their raw content was not saved.

Approved diagnostics sent only fixed synthetic messages/code to the configured
glm-4.7 endpoint, not repository source, logs or memory. Three short baseline
requests returned valid JSON. A long synthetic native-tool history reproduced
textual tool markup as ordinary final text. Its full-history correction hit
the diagnostic 45-second timeout, including a timeout retry. A separate
single-request evidence-packet probe passed strict report-schema validation;
one exact source quote did not match, so this was not an accepted finding.

Final reviewer budget closure and format correction now use a structurally
serialized packet of actual read source ranges and execution context, rather
than continuing the native tool transcript. Prior assistant observations are
bounded and marked if clipped; reasoning and raw invalid output are excluded.
Evidence checks, native handoffs, request/run budgets and the one-correction
limit remain mandatory. Safe diagnostic categories/counts are visible in the
WebUI, without retaining output text or arbitrary provider errors.

Regression coverage includes actual Chat Completions wire conversion, textual
tool markup rejection without tool execution, a two-message correction request,
no unread source leakage, preserved line ranges and prior observations, secret-
free diagnostics, and desktop/mobile diagnostic rendering. Production completion
and live evidence quality remain unverified; no real analysis is retried as part
of deployment.

Validation: 182 backend tests passed, 3 opt-in Docker tests skipped; all 8
browser tests passed. Ruff, TypeScript, production build and whitespace checks
passed. Desktop settings and the 390-pixel mobile diagnostic screenshot were
inspected; the mobile document had no horizontal overflow. The owner workbench
was restarted at http://127.0.0.1:8765 with the original data/repowatch.db after
confirming no queued/running analysis or execution jobs. Startup and HTTP 200
were verified. The isolated browser-test service on port 8766 was stopped.

## GLM-4.7 Finalization Latency Follow-Up (2026-10-14)

The next owner-triggered attempt reached finalization but failed after 573
seconds. A reviewer exploration request consumed about 236 seconds including
one retry; the successful response reported 3,405 reasoning tokens out of 3,464
output tokens. Both final report requests then timed out at 120 seconds.
No final response/token usage exists for these timeouts. This is not another
observed invalid-JSON response and does not establish network failure.

The official GLM-4.7 auto profile now disables thinking only for no-tools
structured finalization. Exploration retains default reasoning. Provider-default
or explicit effort choices opt out. Other models/hosts, Responses, handoffs,
explicit thinking overrides, source-evidence gates and existing retry/deadline
limits remain unchanged. Saved configuration, key and version are not rewritten.
The WebUI displays the derived saved mode and actual request-setting events.

One approved synthetic-only call through the production adapter returned in
9.7 seconds, with 284 output tokens and zero reported reasoning tokens; its
report passed strict schema and exact quote/range validation. No repository
source, execution logs or memory were sent by this diagnostic, and no findings
were persisted. Actual repository completion still requires an owner retry.

Validation: 201 backend tests passed, 3 optional Docker tests skipped; all 8
browser tests passed. Coverage includes strict targeting, legacy auto settings,
explicit provider-default/effort/thinking overrides, unchanged input settings
and credentials, actual Chat Completions wire parameters during finalization
and correction, and unchanged reasoning during tools/handoffs. The first
browser run found an exact-label selector issue in the expanded test; the
selector was corrected and the complete browser suite passed. Desktop mode
settings and 390-pixel mobile timeout-event screenshots were inspected without
horizontal document overflow. Ruff, TypeScript, production build and whitespace
checks passed.

The original data/repowatch.db workbench was restarted at
http://127.0.0.1:8765 after confirming no queued/running analysis or execution
jobs. Startup and HTTP 200 were verified; no actual analysis retry was queued.
The temporary 8766 test service was stopped. No model/provider, encrypted
credential, saved configuration version or historical failure was changed.

## Exact Source Evidence Follow-Up (2026-10-14)

The next owner attempt completed JSON generation, then failed exact source
validation after 399 seconds. Its final response ended normally with 2,909
output tokens and zero reported reasoning tokens. Invalid report text was not
retained, so its exact failed quote/range cannot be recovered. No real retry
or paid model call is initiated by this follow-up.

The fix separates raw source lines from display numbers in the final evidence
packet, accepts citations across contiguous actually-read ranges, and permits
only verified removal of exact line-number display prefixes. Any unread gap,
wrong path, invalid range, fabricated/rewritten quote, or unsuccessful prefix
normalization remains a rejection. Existing valid numeric-prefix literals are
preserved. No fuzzy matching or automatic quote/line relocation is introduced.

One evidence-only correction is available before final SDK output, without
replaying tools/stages or resetting budgets. Diagnostics contain only indexed
fixed categories and counts, not raw invalid output. Correction tools remain
disabled, deadlines apply, and silently empty corrections are rejected.
Final serialization and persistence still revalidate the canonical report.

Validation: 226 backend tests passed, 3 optional Docker tests skipped; all 8
browser tests passed. Regressions cover verified prefix removal, preserved
literal numeric prefixes, contiguous read unions, unread gaps, unknown files,
forged quotes, huge/reversed ranges, bounded evidence correction without tool
replay, combined format/evidence limits, deadline/tool/disclosure gates and
safe indexed diagnostics. Ruff, TypeScript, build and whitespace checks passed.
The 390-pixel mobile diagnostic screenshot was inspected without document
overflow.

The workbench was restarted at http://127.0.0.1:8765 with the original
data/repowatch.db after confirming no queued/running analysis or execution
jobs. Startup and HTTP 200 were verified. No real analysis was retried and no
model calls were made during this fix. The 8766 browser-test service was stopped.
Credentials, saved model settings and historical failure records were retained.

## Output Diagnostics Follow-Up (2026-10-14)


An earlier ModelBehaviorError in reproduction_assistant lacked provider
metadata, so its exact cause cannot be reconstructed. Response hooks and a
read-only adapter now capture safe finish/token metadata and reject truncated
results before execution. Confirmed truncation triggers bounded output
expansion; unknown malformed output is not modified or replayed.
At that increment, 153 backend tests and 8 browser tests passed.

## Timeout Follow-Up (2026-10-13)

Three live failures were subsequently recorded in problem_investigator or
reproduction_assistant as APITimeoutError with the original 45-second request
timeout. The changes increase the request wall-clock/read bounds to 120
seconds and the overall deadline to ten minutes, and add one timeout retry per
request with a shared two-retry limit per run. Completed stages are not replayed.

Six new tests cover transient timeout recovery, unchanged request arguments,
per-call and shared retry ceilings, cancellation during backoff, cancellation
of stalled calls and exclusion of non-timeout errors. Initial fixture tests
and Vite build were blocked by sandbox permissions and an approval-service
HTTP 403. After permission recovery, full regression passed: 146 backend tests,
3 optional Docker skips and 8 browser tests. Ruff, TypeScript, Vite build and
whitespace checks passed.

The updated owner workbench was started at http://127.0.0.1:8765 using the
original data/repowatch.db, with no active tasks at deployment. Existing
credentials, repositories and historical failures were retained. No live
analysis retry was initiated for this follow-up; historical timeout records
remain until the owner starts a new attempt. Live provider completion with
the extended timeout remains unverified.

## Checks

- Full backend regression: 158 passed, 3 opt-in Docker cases skipped.
- Analysis regression includes continuous browsing, parallel source-read
  bursts, character limits, all three native handoffs within reserved turns,
  providers ignoring forced handoff/finalization, classified SDK/provider
  failures, credential-free diagnostics and retry progress isolation.
- Ruff, TypeScript and production frontend build: passed.
- Full browser regression: 8 passed, including failed-analysis status,
  actionable handoff error, stage/duration and retry into a completed report.
- Desktop and 390-pixel mobile report screenshots inspected. No horizontal
  document overflow or browser JavaScript errors in the new flow.
- Git whitespace check: passed.

The SDK tests run real function tools and native handoffs with a deterministic
test model. Browser responses simulate reports and provider saving; they do
not send a real credential to a provider. These establish application behavior,
not a live provider's compatibility or review quality.

## Local Instance

On October 12, the retained repository revision and completed test/start
records automatically produced one analysis task in waiting_model state.
The owner subsequently configured a model. Three attempts on October 12-13
read source but never handed off beyond repository_analyst; their stored
generic errors do not establish the exact exception that ended each attempt.

On October 13, the instance was restarted with stage budgets and improved
diagnostics at http://127.0.0.1:8765. Account/model configuration, source,
execution records and analysis history were retained. The attempted live
retry was rejected by automatic approval review before execution, pending
explicit authorization to transmit the repository source and execution
records to the configured provider. No live model call or retry was made
by this validation session, and no synthetic findings were inserted.

The project preview was stopped; analysis uses saved source and execution
records and does not require the project to remain running. Existing account
connections, configuration, logs and passing test results were retained.

Failed attempts remain available for an explicit retry. Live review acceptance, real
finding quality, repair integration and GitHub publication remain unvalidated
or unimplemented as described in ADR 0017.
