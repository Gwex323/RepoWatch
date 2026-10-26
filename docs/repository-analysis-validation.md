# Repository Analysis Validation


## Authorized Live Pipeline Acceptance (2026-10-16)

After explicit owner consent for destination and source/log disclosure, the
current tested environment continued directly at start. Its new pipeline's
execution-job ledger contains only start: prior setup and passing tests were
retained, not rerun. The demonstration service responded with HTTP 200 and the
configured multi-agent analysis completed. Pipeline state is completed and the
report contains zero new findings. Coverage was bounded to selected backend
source and saved execution records; core orchestration was partly truncated,
frontend/SSE and several backend subsystems were not fully reviewed.

No Issue/PR was created and no finding was confirmed. The existing published
repair and old failed Docker repair were unchanged. The project remains subject
to the existing preview lifetime; starting and analysis success are not a full
end-to-end business-function verification.

## Automatic Execution Pipeline Follow-Up (2026-10-16)

Implemented the durable default download/setup/test/start/HTTP/analysis chain,
manual opt-out, explicit test-only mode, missing-command pause, failure stop and
cancellation without resurrection. Startup-only configuration saves retain the
installed image and valid tests. The atomic save-and-continue operation avoids
reinstallation/retesting and premature analysis between the configuration and
startup requests. Action-specific evidence matching preserves still-valid logs
while analysis reports remain bound to the complete runtime configuration.

Final checks: 535 backend tests passed, ten optional Docker cases skipped;
all eleven browser tests passed; the authored real Docker pipeline test passed
for two revisions, including snapshot reuse and actual HTTP. A loopback HTTP
regression covers single-threaded services. TypeScript/Vite, Ruff and whitespace
checks passed. The real fixture uses a deterministic SDK model, not a live API.

A code-only rollout backed up the original database/logs and restarted the same
workbench. The demonstration start command was saved; installation and the
current passing test were retained. Post-rollout inspection showed ready state,
zero live pipelines, zero pending execution and an unchanged count of three
analysis records. No real project was started. Live source/log transmission to
the configured provider awaits explicit disclosure consent; no real model call,
new Issue or PR was made. The QA service was stopped.



## Environment Recovery and Dependency Reuse Follow-Up (2026-10-16)

The two operational follow-ups are implemented. Repair Docker readiness is now
checked before claiming a model attempt. A transient unavailable engine moves a
new task to `waiting_environment`, stores bounded backoff, and does not call the
model, create a workspace, or increment the attempt. Recovery rechecks the
current auto-repair/model settings, owner confirmation, analysis freshness,
account identity and source digest before queueing. Historical old failures are
not migrated automatically; a narrowly audited, explicit one-time pre-model
recovery is available for the prior bug's exhausted task.

Environment preparation now has two conservative paths. An exact same-revision,
idle, installed environment is returned through a download audit alias rather
than duplicated. A new revision always gets a new source/workspace and no old
test result. Compatible dependency inputs can use a bounded read-only snapshot
captured after successful setup and before test/start. Snapshot compatibility is
bound to repository, account, fixed image content, language/install command and
allowlisted manifest inputs. Python editable/dynamic installs, Go local replaces,
npm lifecycle/local/Git dependencies and unknown inputs use full setup. Copying
checks workbench volume ownership and runs under the normal restricted Docker
policy; failed or foreign snapshots fall back safely.

The UI now distinguishes current Docker readiness from a historical task error,
shows waiting/automatic recovery and preparation reuse states, and refreshes
engine readiness periodically. It never inherits old test evidence and does not
publish or call a model merely because Docker became available.

Final backend validation passed: 513 tests passed and nine opt-in Docker cases
were skipped. Coverage includes stale confirmation, identity and source changes,
source-copy races, bounded wait, one-use historical recovery, exact-version reuse,
dependency compatibility, cache invalidation, foreign volume rejection before
copy, cancellation and helper cleanup. All ten browser tests passed, including
the new states, desktop/mobile screenshots and overflow checks. The three real
Docker dependency-snapshot fixtures (Python, Go and TypeScript) passed on the
final implementation, using only authored examples. Ruff, TypeScript/Vite and
whitespace checks passed. No production repair retry, real model call or GitHub
write was triggered.

After the approved idle-queue check and database/log backup, the personal
workbench was restarted on its original database. The new frontend responded
with 200. Read-only checks confirmed the published task was unchanged and the
old Docker failure remained failed at attempt six, now eligible for an explicit
recovery (can_retry=true). It was not automatically queued. Production snapshot
count was zero, so the first compatible setup still needs to create its snapshot.
The isolated QA server was stopped after validation.


## GitHub Publication Diagnostics Follow-Up (2026-10-15)

The retained DSML task failed at publication on attempt nine, after trusted
verification passed and independent review approved. Its historical error was
`github_denied_or_rate_limited`; no remote mutation intent or publication result
was recorded. Original HTTP status/headers were not retained, so the exact
failed endpoint and historical denial reason cannot be established.

Three explicitly approved GET requests checked the saved identity, repository
metadata and default-branch reference. All returned 200; the final remaining
primary quota was 4,977. Account push/admin permissions do not establish the
credential's Contents write permission, nor does this current observation rule
out a transient historical secondary limit. No GitHub mutation was attempted.

The application now distinguishes token/integration permission denial, other
forbidden responses and throttling. Bounded allowlisted response-header evidence
is retained in repair failure diagnostics and displayed by the UI. Permission
alternatives stay alternatives, rather than being merged into a broader grant.
Known 429 mutation rejections can be reconciled on an explicit retry; uncertain
network outcomes retain the existing no-blind-replay gate.

An approved, tested patch whose publication failed has at most three explicit
publication-only retries, independent of model attempt exhaustion. The saved
production task is policy-eligible, but remains failed and was not requeued.
Regression coverage verifies original model limits, one-use queue grants,
publication ceilings, duplicate requests, credential rotation without identity
switching, freshness and source checks, artifact retention and no model/test
replay. Root causes addressed are missing error distinctions and the implicit
assumption that publication failures must consume the model-repair budget;
the token's actual write authorization still requires owner configuration.

Validation: 63 focused GitHub/publication tests passed. The updated repair
browser test passed on a new isolated QA database, including permission denial,
rate-limit wait hints, alternative permission groups, publication-only retry,
mobile overflow checks and screenshots. TypeScript, Vite production build,
Ruff and whitespace checks passed. The QA server was stopped afterward.
The final full backend regression passed: 478 tests passed and six opt-in
Docker cases were skipped.
No production restart, real model request, production retry or GitHub write
was performed. Live publication success remains unverified.

## Docker And Repair Path Follow-Up (2026-10-15)

The DSML repair's eighth attempt prepared backend/agents/base.py and a new
regression file, then Docker readiness failed before running tests. Docker
Desktop was stopped; starting the installed application restored a local
Linux engine. The JWT repair's fifth attempt failed the safe_path predicate
during submission, with no patch artifact. Its old record lacks the rejected
path and rule, so a specific historical filename cannot be established.

Docker readiness is now checked before model generation and before requeueing
a Docker failure. An offline retry does not mutate attempts or history. The
retained untested patch can resume verification in a fresh workspace, requiring
the same account identity, current evidence, unchanged full source digest,
safe source rereads, canonical schema and workspace validation, every real
verification phase and native independent review. Tests also reject changed
source/accounts and failed regressions before review/publication.

Path errors now retain fixed reasons and an edit index without raw rejected
paths. Only a new regression's protected-word filename may receive at most two
rename corrections; path restrictions, protected content and hash/test gates
remain enforced. Qualified exhausted legacy records gain one explicit, durable
workspace-policy recovery allowance without resets or automatic requeueing.

Validation: full backend regression passed with 453 tests and six optional
Docker tests skipped in that invocation. A separate opt-in invocation then
passed all six real Docker cases for Python, Go and TypeScript execution and
repair (failure before, success after, full suite success and cleanup).
All 10 browser tests passed, including
mobile layout/overflow checks and screenshot inspection. Ruff, TypeScript,
Vite production build and whitespace checks passed. Owner production task
attempts five/eight and model version three remain unchanged. No live model
requests, target-repository test runs, GitHub writes or task retries were
initiated by this validation.

## Patch Parameters And Premature Review Follow-Up (2026-10-15)

Retained attempt seven has no patch or verification artifact. Two submission
calls failed schema validation at patch, followed by a rejected premature
handoff. No regression command ran in this attempt. The old handoff hook
reported repair_tests_failed for missing verification. Its retained events
do not identify whether patch was missing, string-valued or another invalid
type; the exact historical payload cannot be established.

Patch argument handling now losslessly normalizes an exact unwrapped contract
or a once-encoded patch object, then applies the same canonical SDK schema and
workspace checks. Error diagnostics add fixed categories without values.
Native handoff availability depends on trusted passing verification, and a
fabricated premature transfer receives repair_verification_required. A narrowly
qualified exhausted legacy task can explicitly use one recovery attempt while
retaining history, version/evidence checks and monotonic attempt counts.

Validation: all 137 repair-module tests passed, followed by six additional
strict-schema tests (143 total). Coverage includes both transport shapes,
unknown/missing fields, invalid and mismatched hashes, unsafe paths, sensitive
contents, native handoff availability and fabricated transfers, actual failed
regressions, and legacy recovery eligibility/ceilings. Ruff, TypeScript, Vite
build and whitespace checks passed. The repair browser test passed, including
mobile overflow checks and visual screenshot inspection, using an isolated QA
database and mocked responses; no live model requests or GitHub writes are
made. The full backend suite and real Docker cases are not rerun for this
follow-up. Live completion remains unverified.

## Repair Output Exhaustion Follow-Up (2026-10-15)

The next live retry (attempt six) produced no patch. Trusted provider metadata
records two consecutive length-limited outputs: 12,000 tokens with 11,959
reasoning tokens, then 16,000 with 15,922 reasoning tokens. The historical
repair_output_invalid classification hid this specific exhaustion reason.

The exact glm-5.3-flash auto setting now selects low reasoning, extending the
existing glm-5.3 default. Owner-selected reasoning settings and the persisted
provider/model/key/version are retained. Truncation is separately classified,
partial responses never execute tools, and output retries have per-call and
shared run limits. Legacy recovery requires the current attempt's latest two
provider records to prove truncation, no artifacts, current evidence and an
unused old-policy allowance. It is explicit and does not reset attempts.

Final validation: 231 repair/analysis backend tests passed; the repair browser
test passed with mobile overflow checks and visual screenshot inspection.
Mock transport tests verify actual SDK request reasoning and timeouts for
auto/default/high settings. Ruff, TypeScript, Vite production build and git
whitespace checks passed. This follow-up did not rerun the full backend/browser
suites or optional Docker cases. No live model call, GitHub write or production
task retry was initiated. Live low-reasoning provider acceptance and completion
of a repository repair remain unverified.

## Generic Repair Failure Follow-Up (2026-10-15)

The two retained tasks now failed with `repair_failed` at attempts four and
five. Both failures immediately followed a model tool-call response, with no
patch/test event. The historical exception and arguments were not retained;
neither the exact tool nor a specific malformed field can be established.
This is distinct from the earlier request timeout and file-read failures.

Tools now prevalidate using the canonical SDK-generated Pydantic parameter
model. Malformed JSON, missing wrappers, invalid hashes and unknown fields
receive bounded corrective feedback containing only safe field paths and the
static tool schema. Trusted safety exceptions are still fatal. All tool
invocations and classified failures have safe events; provider authentication,
quota, status and connectivity failures no longer collapse into a generic
error. Invalid argument values and raw errors must never enter task history,
server logs or persisted diagnostics.

Failures retain fixed exception categories and bounded application-only frame
locations for later diagnosis. No frame locals, source lines, raw messages,
absolute host paths or external library/repository frames are persisted.

Old exhausted generic failures can explicitly consume one recovery allowance
only when no patch, verification or remote artifact exists and evidence is
still current. The policy and used flag are durable and task versions/history
are preserved. Validation uses mock HTTP/provider/SDK tools and a separate QA
database; it does not reset production attempts or trigger live model calls.

Validation: the full regression passed with 381 backend tests and 6 optional
Docker cases skipped, plus all 10 browser tests. Ruff, TypeScript, the production
build and whitespace checks passed. After the final diagnostic additions,
all 98 repair-module tests and the repair-page browser test passed again.
Model/publication results are simulated;
the exact historical live exception remains unknown.

## Unavailable Source Read Follow-Up (2026-10-15)

The live task ending in `b0252287e65d4c2c` failed on attempt four with
`repair_file_unavailable` after its first model response. The retained events
cannot establish the exact path or candidate flag. Eligible source in the
retained snapshot is valid UTF-8 and below the file limit; the failure must not
be attributed to file size without further evidence.

Source-read failures now produce safe reason/path/candidate diagnostics and
bounded path suggestions. The agent may correct a missing path or read the
original before candidate preparation. Missing, oversized, non-UTF-8 and
OS-unreadable files yield recoverable results. Unsafe paths remain fatal;
every read attempt counts toward the budget and failed reads grant no edit
hash. Original file and patch limits remain unchanged.

Mock SDK tests exercise each failure, subsequent valid reads and repair/review
handoff, denied unsafe paths, bounded repeated failures, absence of raw OS
errors and recovery with/without a prior timeout-policy allowance. Browser
coverage verifies readable diagnostics, the versioned recovery action and
mobile overflow. This validation uses a separate QA database, mock providers
and mock GitHub publication. No production task is reset or requeued.

Validation: 364 backend tests passed; 6 optional Docker cases skipped because
container behavior did not change. All 10 browser tests, Ruff, the TypeScript
production build and git whitespace checks passed. The mobile recovery view
was visually inspected. No live model request or GitHub write was initiated.

## Repair Request Timeout Follow-Up (2026-10-15)

Both live repair tasks reached attempt three with `repair_model_timeout` in
`repair_agent`, before patch submission, tests or publication. Initial tool
responses completed; each subsequent request timed out twice at 120 seconds.
This demonstrates the local timeout boundary, without establishing why the
provider did not finish within it.

The repair HTTP read timeout and SDK stage timeout now both allow 450 seconds.
Analysis keeps its 120-second request timeout. Per-request and shared timeout
retry budgets remain bounded; the 1,800-second coordinator deadline still
cancels in-flight requests. Existing credentials, model and reasoning settings
are preserved. Events/UI expose only safe timing and role metadata.

Old-policy third-attempt timeout failures can explicitly consume one additional
recovery attempt. Evidence and optimistic version checks still apply; the prior
attempt is archived and the one-time allowance persisted. New-policy failures,
unrelated errors and stale evidence cannot acquire this allowance. No failed
production task was reset, requeued or used for validation.

Validation uses mock HTTP transport and real SDK adapters to check outgoing
HTTP timeout values, scaled slow-response/cancellation tests, coordinator
deadline cancellation, recovery eligibility and exhaustion. The full backend
suite passed: 352 tests, with 6 optional Docker cases skipped because container
runtime behavior did not change. All 10 browser tests passed, including the
new timeout progress and recovery button checks.
Ruff, the TypeScript production build and git whitespace checks passed.
No real model call or GitHub write was initiated by this validation.

## Source Safety False-Positive Follow-Up (2026-10-15)

Two live repair attempts stopped on the first source read with
`repair_sensitive_content`, without submitting a patch or executing tests.
The old events did not capture the requested path. A local scan confirmed
that the generic log redactor also matches Python type annotations and
configuration references, including `api_key: str` and `api_key=settings.api_key`.
The repair path incorrectly treated any log-redaction change as a secret.

Source checks now distinguish Python syntax from hardcoded credential values.
Known credential formats and literal defaults remain blocked. A protected read
returns only a safe path, line and fixed rule; its content/hash are withheld,
the read budget is consumed, and no edit is authorized. Other allowed source
can still be inspected. Sensitive patch output aborts before tests/publication.
Log and analysis-snapshot redaction retain the original behavior.

Validation: 344 backend tests passed; 6 optional Docker tests were skipped in
this run because the runtime policy did not change. All 10 browser tests,
Ruff and the TypeScript production build passed. New SDK tests verify that
blocked content never reaches the next model input or persisted task record,
and that sensitive patches cannot trigger tests or publication. Existing
failed live attempts and owner confirmations are preserved. No live retry or
GitHub write was initiated by the validation.

## Automatic Repair Follow-Up (2026-10-14)

Issue #3 is implemented on `feature/automatic-repair`. A confirmed finding
queues a revision-bound repair task. The repair agent can only read bounded
non-sensitive source and submit complete source replacements plus new
regression files; existing tests, manifests, CI and credential paths remain
protected. Python, Go and TypeScript authored fixtures run in the restricted
Docker runtime and verify the required fail-before/pass-after/full-suite
contract. The independent reviewer receives the trusted diff and results
through a native SDK handoff. A disabled-on-upgrade installation switch
controls automatic work; owner feedback is the only discovery checkpoint and
the GitHub repository owner remains the only merge actor.

Validation: 303 backend tests passed, with 6 optional Docker cases separately
enabled and passed; 10 Playwright browser tests passed. Ruff and the production
TypeScript build passed. Desktop progress/settings and 390-pixel mobile
PR history/evidence screenshots were inspected. Remote publication tests use
an HTTP mock; no live model request or live GitHub write was initiated.

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


## 2026-10-16: Cross-revision historical finding rechecks (ADR 0021)

New analyses now freeze up to eight same-repository historical findings with
waiting_reproduction/confirmed human status, across revisions. Confirmed claims
come first. Counts disclose cap omissions. Input descriptions are bounded and
redacted, with clipping noted. The snapshot and its origin versions persist across
retries. Exact-revision memories keep their existing separate contract.

The structured report carries separate historical_rechecks outcomes: still_present,
fixed, suspected_false_positive, not_covered. Substantive opinions require current,
actually read source quotes. Unknown/duplicate IDs and forged/stale/unread evidence
are rejected under the existing bounded evidence-correction mechanism. Missing
per-item decisions are explicitly not_covered, never assumed fixed. The reviewer
must check whether an old reproduction actually supports its claim; rejection of
an all-whitespace secret is not, by itself, a validation bypass.

The read-only UI retains origin revision, finding ID/version and original human
status alongside model opinion, rationale and current evidence. No new findings is
explicitly not a declaration that all historical issues are resolved. Old completed
reports are not rewritten. No human feedback, repair task or GitHub write is changed
by recheck output. Existing cancellation/staleness gates protect both findings and
historical outcomes against late completion.

A read-only selection against the personal database confirmed that both existing
DSML and JWT findings are candidates (two included, none omitted). This checked only
input selection, not correctness of either finding or a new model conclusion. No
real model call was made, and no personal task was restarted for validation.

Validation completed: 29 new historical-review tests; 145 combined analysis tests;
564 backend tests passed with ten opt-in Docker tests skipped; all eleven browser
tests passed on the isolated QA instance. TypeScript/Vite, Ruff and whitespace
checks passed. Mobile historical-review screenshots were visually checked with
no overflow. The isolated QA server was stopped after identity verification. The
personal backend was not restarted; actual historical opinions require a backend
restart and a newly run analysis, not reinterpretation of the old empty report.


## 2026-10-16: Execution-read prerequisite regression

Readonly audit of analysis_e5aadbcb99144fc1 found three actual handoffs, two source
reads, and no execution-record read. Its final generic analysis_review_incomplete
error was the missing execution read, not failed stage handoffs. No report/findings
were retained. Source-quote correction had already consumed its one bounded retry.

The analyst now must request the actual execution-record tool and then source tool
before handoffs are offered. Forced responses are validated, and failed/invalid
source reads remain bounded by the original stage/deadline/read limits. Missing
prerequisites and ignored named-tool choices have distinct, credential-free errors
and UI labels. This does not bypass report validation, synthesize audit evidence,
change human confirmations, replay old failures, or trigger a live retry.


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


## 2026-10-16: Owner-requested removal of built-in samples

Added local-only Store.remove_samples maintenance with one SQLite transaction and
foreign-key enforcement. It scopes deletion by repositories.kind=fixture, removes
fixture runs/jobs/events/artifacts and workflow-only memories/history/read references,
and records samples_enabled=false so initialization cannot recreate the repository.
Active sample jobs, owner-authored sample notes, sample environments and cross-repo
references block cleanup without partial deletion. No public cleanup endpoint was
introduced. Sample launch APIs cannot recreate a disabled fixture. GitHub repositories
are never selected merely because their name resembles a sample.

The UI hides sample launchers/settings/role labels when no fixture repository exists,
uses real published-PR counts instead of sample-completion statistics, and retains
real repository controls. Developer fixtures remain usable in isolated test databases.

Validation: 582 backend tests passed, ten opt-in Docker tests skipped (442.77 seconds).
All twelve browser tests passed, including a new sample-free desktop/mobile case.
The first browser run found an incorrect selector in the new test, corrected before
the complete rerun. TypeScript/Vite, Ruff and whitespace checks passed. The mobile
sample-free screenshot was visually checked. The isolated QA instance was stopped.

Before personal cleanup, the database/logs were backed up to
 data/backups/remove-samples-20261016-150341. A separate copy was cleaned and reopened
to verify non-reseeding and unchanged protected-table hashes. With no active tasks,
the identity-checked personal backend was stopped, cleanup applied, and the service
restarted at port 8765. Exactly one fixture repository, one paused sample run, one
sample job and seven sample events were removed; there were no sample memories or
artifacts. Restart preserved the disable marker and did not reintroduce any sample.

All 27 protected table projections matched their pre-cleanup hashes after restart,
including real repositories, model/GitHub configuration, sessions, analyses, findings,
repair records and environments. Integrity and foreign-key checks passed, HTTP was
200 and startup logs showed no errors. The personal workspace retains one GitHub
repository, five analyses, two findings and two repair tasks. No live model call,
project test, environment reinstall, remote deletion or GitHub write was initiated.


## 2026-10-16: Stale installation advice is historical, not a current failure

Readonly audit confirmed installed=true, a completed latest start job, and both
Diagnoses.latest.current/applicable=false for the old setup diagnosis. The UI had
rendered every non-null diagnosis as an expanded failure card regardless of that
current flag; only the small historical tag and disabled apply button differed.
Backend expiry/apply guards were correct, but presentation did not honor the same
contract. The prior browser test even required the obsolete card to remain visible.

Frontend-only fix: an active failure card requires a current diagnosis, a failure
environment state and no active execution. Otherwise it becomes an initially closed,
read-only historical details section with an explicit non-current disclaimer. Old
patch values and evidence remain available for inspection, but no reinstall action,
current-model spinner or unverified-current-advice footer is rendered. A separate
installed-dependency label reflects the existing installed flag. Later current test
or startup failures must not be suppressed just because dependencies are installed.

Regression coverage now transitions through applying a recommendation, setup in
progress, recovered dependencies/passing tests, running, stopped, late stale model
results, and a new test failure with installed=true. It checks collapsed defaults,
read-only expansion, absent reinstall/spinner controls and foreground display of
the new failure. No personal data, project execution or model calls are changed.


This frontend-only follow-up passed 22 diagnosis backend regressions and all twelve
browser tests (including the expanded stale-diagnosis recovery case), plus the
TypeScript/Vite build, Ruff and whitespace checks. The recovered mobile screenshot
was visually inspected; historical advice was collapsed with the dependency-ready
and passing-test indicators visible. The personal instance served the updated UI
with HTTP 200 without a backend restart. The isolated QA instance on port 8794 was
stopped and its port verified closed. No real project test, reinstall, model call
or personal database mutation was initiated by this validation.


## 2026-10-16: Source-request compatibility and misleading pipeline fallback

The generic refresh message came from an unmapped pipeline analysis error, not a
failed retry HTTP request: both owner retry POSTs returned 200, then analysis stopped
at source-request validation after an execution-record read. Prior events do not
contain response call counts, so the exact rejected shapes are unknown.

Mandatory pure-source batches are now accepted within the remaining source-read
budget; mixed/empty/oversized calls remain rejected before SDK execution. Execution
reads remain single-call. Request diagnostics retain only fixed labels/numeric counts
and do not replace actual read events. Analysis/pipeline UI messages share one table.
Regression validation passed 212 targeted backend tests and all twelve browser tests,
plus build/lint/whitespace checks. The isolated QA server was stopped. Verified backup
and code-only rollout on port 8765 preserved all 27 database-table hashes, task states,
credentials and model-call history. No live analysis or project operation was started.
See the bounded-batch follow-up in ADR 0021 for limits and uncertainty about old calls.


## 2026-10-16: Premature investigator answer and bounded native continuation

The latest attempt read execution records and two real source files and completed
only repository_analyst -> problem_investigator. A plain final answer from the
investigator stopped the SDK before reproduction/review, producing
analysis_handoff_incomplete. The preceding source-batch compatibility change was
working; this was a distinct intermediate-stage completion problem.

Final-report instructions are now limited to the reviewer. Any premature nonfinal
answer receives one role-scoped closing opportunity in the same in-memory attempt,
resuming the SDK conversation and preserving real read results/observations. The
provider must emit the actual native handoff; no read replay or invented audit event
is permitted. Original role/deadline/total-turn limits, explicit-refusal handling,
final review and cancellation/config/CAS guards remain effective. Failed historical
attempts are preserved; deployment does not automatically resume or retry them.

Ten new local regressions cover every nonfinal role, all early roles in one attempt,
context retention, no repeated reads, noncompliance, refusal, evidence forgery, total
turn exhaustion, persistence and cancellation. Targeted analysis regression passed
143 tests before the additional total-turn test; all ten final recovery regressions
passed afterward. Full backend and browser results are recorded after completion.


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


## 2026-10-16: Python desktop offline branch (ADR 0022)

Implemented AST-only Tkinter/stdlib-layout recognition, bounded transitive import
inspection, explicit compatible unittest selection and GUI/native/unknown exclusions.
First execution requires owner acknowledgement; approved offline mode can persist
per repository/account. Empty legacy downloads can be re-detected without executing
anything or overwriting custom commands. Desktop jobs cannot start GUI/HTTP; all-
skipped/zero/unrecognized execution cannot count as passing. Reports retain partial
scope from saved test evidence independently of model claims.

Real isolated Docker validation before interruption passed a trusted fixture and the
cached desktop project's six candidate modules: 109 executed cases passed, two were
skipped, and eleven whole modules were excluded. No host/client configuration was
mounted and no paid model was called. The two initial QA dependency caches were
explicitly removed under non-personal namespaces. A later fixture rerun's final tool
result was unavailable after interruption and is not another claimed pass. Docker
was unavailable on resume; no additional real-container verification was performed.

Fresh final validation: 624 backend tests passed, twelve optional Docker tests skipped,
and all thirteen browser tests passed. Desktop/mobile scope warnings were visually
checked. Build/lint/whitespace passed. The personal database was backed up, then only
its blank-command desktop proposal and waiting reason were updated. It remains
unapproved/uninstalled, with no formal test result and only its original download job.
The workbench was restored at port 8765, all 27 protected table projections matched,
integrity/FK checks passed, and QA port 8797 was closed. No formal desktop pipeline,
model call, source modification, repair, or GitHub write was triggered. See ADR 0022
for precise scope, API constraints, limits and the backup path.

## 2026-10-23：最终审查预算与检查点恢复（Issue #61）

从主干 `51be49f` 实现 ADR 0061。26 项新增离线恢复测试覆盖六项需求预算、上限与预留时间、
真实工具读取后的范围合并、完整需求及限制保留、观察裁剪标记、重启后只调用审查员、
模型/密钥/Issue/范围版本/协议/源码/执行变化失效、取消竞争、引用造假拒绝、审查读取刷新
检查点与旧任务无检查点的完整重试。恢复报告必须经过原有 schema 和精确源码校验。

验证记录（均为隔离测试，未调用真实供应商）：

- 首次完整后端回归：892 通过、29 失败、12 个可选 Docker 测试跳过，735.39 秒。
  其中 24 项是 GLM 用例对旧逐行证据结构的断言；已改为合并文本块，未删减引用校验。
  另 4 项修复预算单测在未修改的主干 `51be49f` 上复现同样的
  `repair_review_evidence_required`，补齐“已读取审查依据”的测试前置条件后通过。
  余下 1 项是临时 JSON 文件替换的 Windows 权限错误，重新运行后通过。
- 修正后重跑检查点、完整 GLM 兼容和记忆验证三个模块：77 通过，46.59 秒。
  重跑相关修复预算用例：8 通过，3.23 秒。最终 pytest 失败缓存为空；没有把首次全量
  回归表述为一次全绿。随后新增的两个损坏类型用例已包含在上述 77 项复测中。
- 三项分析页面浏览器回归全部通过，34.5 秒：普通重试、检查点恢复、历史问题人工处置。
  修正已有浏览器用例对下拉框、独立配置折叠区和异步开关的定位与等待方式。
  桌面/移动端截图已检查，页面无横向溢出，恢复按钮与事件提示按状态更新。
- Ruff、Python 编译、TypeScript/Vite 构建及 `git diff --check` 通过。

临时浏览器服务使用分支目录下的新库 `data/report-recovery-browser-qa.db` 和端口 8780，
验证后按 PID/命令行核对并关闭。生产 8765 服务及其个人数据库未修改。
未重跑 `gateway-model-assistant` Issue #2，未在目标仓库创建 Issue/PR。
旧失败记录在更新前没有检查点，更新后的首次重试仍需完整分析；不得用旧报告补造检查点。
PR #60 的范围模块仍待独立合并，后续集成须保留范围 schema、工具及最终验收门禁。

## 2026-10-26：工具参数、角色权限与高推理预算补全（Issue #71）

新增 28 项离线回归，覆盖嵌套枚举和未知字段脱敏、JSON 数值位置、1–3 项范围批次、
同一角色权限策略、工具批次执行原子性、同轮证据保留、跨角色共享两次纠正、疑似输出
触顶扩容和保持 high、合法 JSON/字段错误不误扩容、额度耗尽、取消/期限、范围真实登记
后交接、持久化追加/去重/不可弱化、完整诊断存储，以及范围报告遗漏的有界纠正和
高推理报告空间/时间预留。更新旧模型夹具以执行真实范围工具、返回有效范围结论；
检查点范围测试改用已存在的真实表与外键，未放宽生产权限或证据校验。

最终相关回归：269 passed，118.68 秒。覆盖工具参数、仓库分析、GLM 兼容、检查点恢复、
交接恢复和修复反馈六个模块。Ruff、Python 编译、TypeScript/Vite 构建及 whitespace
检查通过。这里记录的是相关模块回归，没有声明全仓库测试全量通过。

真实验证使用生产库在线只读备份，重放 `analysis_9da31ce04adb4b90`，源码 revision
`f7db1d4ca234805e324450889787d5bac335c5cd`，源码内容摘要
`7cfd3c4737fb89135b31ce7ed869a46286daa8b1638b4f4886fe2ba993e64080`。
有效配置为仓库分析 `gpt-6.1-sol`、其余分析角色 `glm-5.3`，均为 chat_completions/high。
隔离副本只运行选定分析服务，禁用其他队列发现，不运行修复或发布，不修改原失败记录。
Windows 凭据库解密仅在内存中使用；没有输出密钥、原始工具参数或模型报告正文。

第一次真实重放完成四角色、三次交接、16 次请求，没有工具参数错误，但 scope_review
为空，不能当成完整验收；据此新增报告范围契约。第二次实际执行四角色和三次交接，
范围报告契约通过，随后进入证据纠正；该请求明确 length 截断，8000 输出中推理 7990，
最终由十分钟总期限停止，未保存不合格报告。依据该证据补充高推理报告预算，并通过
正式 retry 服务在隔离库中验收真实检查点恢复。

高推理报告预算补充后，两轮真实检查点恢复都进入独立审查，输出预算为 11300，
预留时间 360 秒，保持 glm-5.3/high。每轮均发生两次 120 秒请求超时后停止，最终错误
为 analysis_provider_timeout_exhausted；未再出现工具参数错误。超时本身不能区分模型
服务排队、推理耗时或网络原因。没有调整模型、降低 high、扩大请求期限或弱化证据
校验来制造通过结果。因此完整真实验收仍未通过，不把本轮记作端到端修复验收通过。

保留 PR60 自动返工与 Langfuse 的集成提交 62d5bec 构建/静态检查通过，相关回归同样
269 passed，130.41 秒。未切换正在运行的工作台（仍为 3cd745f），也未合并 GitHub PR。
生产队列检查为空；在线备份 workbench-before-tools71-complete-20261026-162830.db，
quick_check=ok。核验会话接口、首页及全部首页资源 HTTP 200，启动日志无 Traceback；
供应商、角色配置、模型总开关、GitHub 账号、仓库、旧分析与问题表共八项指纹保持一致。

隔离运行的 Langfuse metadata_only 初始化并产生本地关联 trace ID，没有监控初始化
错误。云端旧 trace 查询接口返回 HTTP 410，本轮未确认云端入库，不能据此宣称追踪已
完成云端验收，也不能把 410 推断为导出失败。待供应商请求可以在既定期限内完成后，
使用保留的真实检查点继续验收，通过后再部署已准备的集成版本。
