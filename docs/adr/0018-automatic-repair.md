# ADR 0018: Evidence-Bound Automatic Repair

Date: 2026-10-14
Status: implemented for Issue #3; live provider/remote acceptance remains explicit

## Scope And Gates

Continue confirmed real-repository findings into isolated repair, regression
verification, independent SDK review and GitHub publication. The original
download and execution environment remain unchanged. Existing-Issue intake
remains a separate increment; its automatic-validation gate is unchanged.

A unique durable repair belongs to one finding and the exact analysis revision,
configuration fingerprint and execution evidence. Authenticated owner feedback
atomically queues a repair. A reconciliation pass backfills still-current older
confirmations. Negative, stale and unconfirmed findings never authorize repair.

One installation-level switch enables automated repair and publication. It is
off on upgrade so deploying this capability cannot silently expand the effects
of an earlier confirmation. Once enabled, no per-patch or publication approval
is required. The switch can pause the queue and interrupt active local work.
GitHub merges and auto-merge are never implemented.

## Source Screening Follow-Up (2026-10-15)

Log redaction is deliberately broad and cannot serve as a source-code safety
predicate. Python annotations such as `api_key: str` and references such as
`api_key=settings.api_key` previously caused both live attempts to stop on the
first source tool call. Historical events did not retain a path, so the exact
original file cannot be established retrospectively.

Source screening now uses Python AST and lexical tokens to distinguish actual
code symbols and type annotations from credential literals, including function
defaults, typed assignments, environment fallbacks and field defaults. Known
credential patterns remain blocked before any syntax exemptions. Invalid
Python and other languages retain conservative assignment checks. Log
redaction and analysis snapshot behavior remain unchanged.

A blocked read returns only a fixed error, validated path, line and rule, never
content or a file hash. It consumes the read budget and does not authorize an
edit; the agent may inspect other files. Sensitive patch content still stops
the attempt before execution or publication. The WebUI displays only safe
path/line/rule diagnostics. Tests must cover both false positives and real
credential rejection at the workspace, SDK-tool and persistence boundaries.

## Model Timeout Follow-Up (2026-10-15)

Two live third attempts received an initial tool response, then each hit the
120-second request limit twice before submitting a patch. The repair wrapper
and HTTP client shared that analysis-oriented limit. These events establish
local timeout exhaustion, not the underlying provider or network cause.

Repair requests now use a shared 450-second limit in both the HTTP client and
the stage wrapper. Analysis retains 120 seconds. Each logical request can still
retry a timeout once, with at most two extra timeout requests across the run;
the coordinator still cancels the entire repair at 1,800 seconds. Model,
reasoning configuration and credentials are preserved. Safe request events
record role, turn, request index, timeout limit and elapsed seconds, never
request bodies, reasoning, tool arguments or raw provider errors.

The existing three-attempt limit remains the default. A failed third attempt
whose last error is `repair_model_timeout` and whose recorded request policy
predates this fix may explicitly consume one additional recovery attempt.
Recovery checks current owner confirmation/evidence and task version, archives
the previous attempt and records its one-time policy allowance in task context.
Attempt numbers remain monotonic. New-policy failures, other error classes,
stale evidence and exhausted recovery cannot receive this allowance. Failed
tasks are never automatically requeued by deployment.

## Recoverable Source Reads Follow-Up (2026-10-15)

One live recovery attempt received its first tool response and then failed with
`repair_file_unavailable`. The historical event did not retain the requested
path or candidate flag. All eligible source files in that retained snapshot
are valid UTF-8 and below 32,000 bytes, so the record cannot establish whether
the model requested a missing path or a candidate before patch preparation.

Unavailable source reads now return a fixed reason, safe path, candidate flag
and bounded original-source path suggestions to the agent. Missing files,
unprepared candidates, oversized files, invalid UTF-8 and OS read errors are
recoverable tool results. The UI shows the same safe diagnostics. Unsafe paths,
sensitive content policy, read/character budgets and exact-hash edit gates
remain enforced. Every attempted read consumes the read count; failed reads
never authorize edits or return source content/hash. Reads are capped at
32,001 bytes to enforce the existing 32,000-byte limit during a size race.

An exhausted old-policy `repair_file_unavailable` failure can explicitly consume
one source-read recovery attempt, including a task that already consumed the
timeout-policy allowance. Its source-read policy and used flag are durable;
normal limits, version/evidence checks and archived attempt history still apply.
New-policy failures and other errors do not receive this allowance. Deployment
does not requeue tasks or initiate model/publication calls.

## Tool Arguments And Failure Classification (2026-10-15)

Two retained live attempts ended with `repair_failed` immediately after a
successful model tool-call response, without a recorded patch/test event. The
old handler discarded the exception type. This timing identifies the tool
dispatch/validation/execution boundary, but cannot retrospectively establish
the exact exception or invalid field.

Repair tools now prevalidate arguments using the SDK's own generated Pydantic
parameter model before invoking the SDK tool body. Invalid JSON and schema
errors return the exact tool schema plus safe field paths to the model. There
are at most two corrections shared by all repair/review tools in one run;
the third invalid invocation fails with `repair_tool_arguments_invalid`.
Neither malformed arguments nor their error handlers execute a tool body,
modify a workspace, run tests or publish. Trusted safety/evidence exceptions
from a valid invocation remain fatal and never become format corrections.

Events record only fixed tool names, fixed reasons, contract field names and
remaining correction count. Arbitrary extra keys, values, source, provider
bodies and raw exception messages are excluded. Tool invocation failures and
SDK/provider failures receive safe classified codes, including authentication,
rate limits, connectivity, request rejection, output errors and filesystem IO.
Exception chains preserve trusted repair/GitHub/Docker gates. SDK tracing also
explicitly excludes sensitive data.

Failure events also retain fixed exception categories and at most eight frame
locations inside the application package. Only repository-relative application
filenames and line numbers are retained: no exception message, source line,
traceback local, absolute host path or external SDK/repository frame is stored.
This preserves useful diagnostic evidence if an unclassified error recurs.

An exhausted old-policy generic failure with no patch, verification or remote
artifact may explicitly consume one classified-failure recovery attempt.
Prior timeout/source allowances remain monotonic; the new used flag and policy
are durable. Artifacts, stale evidence and new-policy failures cannot receive
this allowance. No deployment starts paid model calls or remote publication.

## Reasoning Exhaustion Follow-Up (2026-10-15)

The sixth retained attempt of repair_b0252287e65d4c2c failed before patch
submission. Its third logical request ended with finish_reason=length twice:
12,000 output tokens (11,959 reasoning), then 16,000 (15,922 reasoning).
These records establish output-budget exhaustion, not malformed patch JSON.
The old adapter mapped repeated truncation to repair_output_invalid and auto
reasoning selected low only for glm-5.3, excluding the configured flash variant.

Auto reasoning now also selects low for the exact glm-5.3-flash model name.
Explicit default/high/other owner settings, unrelated model names, provider,
key and saved model version remain unchanged. This is an application default;
live provider acceptance and repair completion remain unverified. Truncated
responses are discarded before tool execution and classified separately as
repair_output_truncated. A retry asks for brief reasoning and the next complete
action while retaining evidence and safety gates. There is one output retry
per logical call and two shared across the whole repair/review run; the existing
16,000 token cap, request deadline and overall deadline remain in force.

An exhausted legacy repair_output_invalid task may explicitly consume one
output-policy recovery attempt only when its latest two provider records in
the current attempt both confirm truncation and no patch, verification or
remote artifact exists. Current-policy tasks, previously used allowances,
old-attempt evidence and generic JSON failures do not qualify. Source/version
checks and archived monotonic attempt numbers remain enforced. The WebUI shows
the actual truncation reason and safe output/reasoning counts. Deployment does
not retry tasks, call live models or publish GitHub content.

## Patch Protocol And Premature Handoff (2026-10-15)

The seventh retained attempt of repair_b0252287e65d4c2c failed before any patch
or test execution. Two submit_and_test_repair invocations failed argument
validation at patch, followed by a model tool response and the SDK handoff hook
rejecting missing verification. The old hook reported repair_tests_failed,
misrepresenting absent test evidence as an executed regression failure. Raw
arguments and Pydantic error categories were not retained, so the exact old
argument shape cannot be reconstructed.

The submit tool now normalizes only two lossless JSON transport shapes: an
object with exactly the four Patch contract keys gains the patch wrapper;
a root object containing only a string-valued patch may decode that string
once when it contains a JSON object. The canonical SDK parameter schema must
still validate before invocation; workspace path, hash, content, regression
and test gates remain mandatory. No missing content/hash is fabricated,
unknown keys are not dropped, and malformed JSON is not repaired. Safe events
record only the fixed normalized shape or fixed validation categories.

The native SDK handoff is_enabled predicate exposes independent review only
after trusted verification passes. A fabricated early transfer call or an
exhausted exploration stage without verification fails specifically with
repair_verification_required. The hook and verification reader distinguish
missing records from actual failed tests. Argument errors explicitly direct
the agent to correct submission and wait for passing tests before transfer.

An exhausted legacy repair_tests_failed record may explicitly consume one
protocol recovery attempt only with no patch/verification/remote artifact,
at least two patch-level schema failures in its current attempt, no recorded
patch/test/handoff event, current evidence and an unused old-policy allowance.
Attempts and history remain monotonic. New-policy failures cannot requalify.
Deployment starts no live model requests or GitHub publication.

## Docker Preflight, Retained Patches And Path Diagnostics (2026-10-15)

Two live attempts subsequently failed for different reasons. The DSML repair
prepared a patch but Docker readiness failed before any test ran. Docker
Desktop was not running; starting the installed application restored the
Linux engine. The JWT repair failed inside safe_path during patch preparation.
That historical record contains neither the rejected path nor a fixed reason,
so the exact path/rule cannot be established retrospectively.

Repair now checks Docker readiness before model generation. An explicit retry
of a Docker-unavailable task checks readiness before queue/history/attempt
mutation. A saved patch that failed readiness before testing may be retained
for verification: bind the same GitHub identity, recheck current confirmation
and source evidence, compare the full source digest, reread edited originals,
and reapply the canonical Patch and workspace validators in a fresh attempt.
Run every original/baseline/candidate verification phase before model handoff
and independent review. A verified publication resume remains separate and
does not require Docker. Failed tests, review vetoes and other errors retain
the existing regeneration behavior.

Path rejection now records a fixed reason and optional edit index, never the
raw rejected path. Traversal, absolute/Windows syntax, symlinks, protected
directories/files, disallowed types, duplicate names and case aliases remain
blocked. A new regression filename containing a protected sensitive word may
receive at most two naming corrections per run, only with an empty original
hash and a path not present in the original snapshot. No failed proposal is
accepted or tested. The model must choose a neutral filename and update its
edit, regression_paths and command consistently. Existing source paths and
other path failures remain fatal; all content/hash/test gates are unchanged.

An exhausted old-policy Docker-unavailable task with an untested retained
patch, or an artifact-free unsafe-path failure proven to originate from
submission in its current attempt, may explicitly use one workspace recovery
allowance. Used flags, current-policy failures, stale evidence and artifacts
outside this narrow resume contract cannot qualify. Attempt numbers/history
remain monotonic; deployment does not requeue production tasks or publish.

### Rejected Source File Types (2026-10-21, Issue #40)

The second live repair attempt failed inside read_repair_source before any patch
submission. Application frames identify safe_path's unsupported-file-type guard;
the actual requested path was not retained. The shared repair_unsafe_path UI text
incorrectly described a patch failure. This is distinct from the earlier attempt's
failed regression tests and the resolved GLM report-format incompatibility.

An unsupported source-read type now returns a bounded correction result with
actual eligible source paths. At most two such corrections are allowed per run;
the third rejection terminates with repair_source_path_rejected. Rejected reads
still consume the existing read and stage budgets. No rejected file is read,
hashed or counted as evidence, and patch file types are unchanged. Every other
path rejection remains fatal. Events retain only the fixed reason, correction
count and recoverability, never the raw rejected path or file contents. The UI
distinguishes read-path events from patch-path events, and the generic path error
no longer claims a patch was submitted. Deployment does not retry failed tasks.

## Execution And Review

Use the existing configured model through its existing SDK/provider adapter.
The repair agent reads bounded, non-sensitive source and submits one structured
set of file replacements plus new regression files. Exact content hashes bind
edits to files actually read. Paths, sizes and file types are checked outside
the model; existing tests, dependency manifests, credentials and CI files are
not editable in this first repair increment.

Host operations only copy and edit validated files. Repository commands run
inside the existing non-root, resource-limited Docker policy with no host
mounts, publishing credentials or Docker socket. Installation uses only the
owner's recorded network choice; all tests are offline.

Record the original suite, the new targeted test against the original code,
the targeted test against the patch and the full patched suite. Require
failure before and success after, plus a passing full suite. An independent
reviewer receives the actual diff and trusted test records via a native
handoff and must approve before publication. Neither passing tests alone nor
model-authored assertions replace the owner gate or independent review.

## Persistence And Publication

Persist state, attempt limits, leases, bounded events, diff, test records,
review and remote identifiers. A crashed local attempt is interrupted and
requires an explicit bounded retry. A verified result can resume publication
without replaying source edits or tests; source/evidence must still be current.

Publish through a trusted GitHub REST adapter, not a tool available to the
model. Verify the bound account and current target revision. Use a dedicated
feature branch, or an authorized public fork when upstream push is unavailable.
Create content-addressed Git objects and reconcile deterministic refs. Store
an intent before non-idempotent writes and reconcile Issue/PR markers after
uncertain outcomes; an unresolved write is not automatically repeated.

## Validation

Backend tests cover queue deduplication, backfill, stale/negative gates,
path and hash validation, unchanged original source, test ordering and failures,
real SDK handoff, model budget failures, review veto, credentials, publication
reconciliation and owner/CSRF API checks. Browser tests cover paused/configured
queues, progress, evidence, retry, PR links and desktop/mobile overflow.
Python, Go and TypeScript authored fixtures exercise regression contracts.
The current local suite has 303 backend tests passed, 6 optional real Docker
cases separately enabled and passed, and 10 browser tests passed.
Ruff and the TypeScript production build also passed.
Live provider calls and remote writes require separate explicit live acceptance;
mock results are never inserted into the owner's production records.


## GitHub Access Diagnostics And Publication Recovery (2026-10-15)

A retained attempt reached publication with passing tests and approved review,
then failed with the historical combined GitHub access/rate-limit code. The
original response was not retained, so its exact failing API call and permission
cannot be established retrospectively. A three-GET diagnostic confirmed current
identity, repository metadata and default-branch access; remaining primary quota
was 4,977. Repository account permissions are not token write permissions.

Access errors now distinguish explicit token/integration permission denial,
otherwise-forbidden responses and primary/secondary throttling. Only bounded
allowlisted status, operation category, required-permission headers, quota,
retry timing and request ID enter repair failure evidence; no raw response,
repository path or credential is retained. A definite HTTP 429 rejection marks
the publication mutation rejected rather than ambiguously pending, permitting
an explicit reconciled retry without blindly replaying an uncertain POST.

Verified publication failures have a separate maximum of three explicit retries,
including old tasks whose model attempts were exhausted. Each request consumes
one durable allowance and queues a one-use publication-only claim. It never
extends model-repair budgets or clears patch/test/review artifacts. Existing
confirmation, evidence freshness, same-identity credential refresh, source
fingerprint, remote revision and publication reconciliation gates remain.
Neither deployment nor inspection requeues or publishes production tasks.

## Verification Feedback And Candidate Revisions (2026-10-21, Issue #42)

A live patch made its two new regression cases pass but broke one of the 111
existing tests. Verification previously raised out of the SDK tool, so the model
never saw the failed assertion. Single-submission workspaces and treating every
verification object as a closing signal also prevented same-attempt correction.

Complete failed verification now returns bounded, redacted logs and actual exit
codes to the repair model. It must diagnose implementation/test/contract conflicts
before submitting a correction; existing tests and publication gates stay protected.
The first two failures permit corrections, with at most three candidate revisions.
An identical implementation/test/command proposal is not executed again even if
its summary or edit ordering changes. An unresolved final explanation is recorded
as an unverified model judgment, never a successful repair.

Each revision is validated against the same immutable original hashes and built
in fresh baseline/candidate directories. Prior candidates remain on disk; complete
patches, diffs and test evidence are persisted as verification round history before
the next model request. Current verification and prior rounds are separate API
fields. Only passed current verification can enable native reviewer handoff.

Each permitted correction adds three bounded model exploration calls. Shared
timeout/output retries, source-read budgets, pause/cancellation checks and the
overall thirty-minute deadline are not reset. Infrastructure/setup errors and
path/content violations do not enter this test-feedback loop. No unchanged patch,
failed round, model explanation or prior passing candidate authorizes publication.

An exhausted old-policy task with complete failed verification, a retained patch,
source digest and no review/publication can explicitly consume one upgrade retry.
Current-policy/used/partial/verified/stale tasks cannot gain this allowance. The
previous attempt snapshot and bounded failure evidence are preserved for the next
repair model. Deployment never queues that retry. The UI displays each candidate,
feedback progress and the opt-in upgrade action without exposing raw reasoning.

### Integration With Current Repair Policies (2026-10-22)

Verification correction and reviewer evidence recovery retain separate policy
markers and one-time allowances. A failed candidate cannot authorize handoff;
after a passed candidate, the reviewer must actually read trusted verification
before its final schema is enabled. Missing evidence keeps the specific
`repair_review_evidence_required` error. Candidate corrections share the existing
output/timeout budgets and preserve per-role model and reasoning selections.

## Adaptive Output Recovery (2026-10-22, Issue #51)

A retained repair attempt exhausted 12,000 output tokens (7,073 reasoning),
then 16,000 (15,992 reasoning). Increasing the output cap alone left almost
no budget for the actual tool arguments. Truncated responses remain discarded
before tool execution.

After an explicit truncation, the existing bounded retry temporarily reduces
medium/high/xhigh reasoning to low and keeps that reduction for subsequent
calls by that same repair/review agent. It copies request settings without
changing persisted owner configuration, provider, key, model, other reasoning
options or tool/handoff settings. Default reasoning is left unspecified to
avoid adding unsupported provider parameters; low/minimal/none are preserved.
The output cap remains 16,000, with one output retry per logical call and two
shared across the run; request and overall deadlines remain unchanged.
A fixed-field event records the temporary reduction for the workbench.

The adaptive policy also permits one explicit recovery for an exhausted
old-policy repair_output_invalid or repair_output_truncated task only when
its latest two provider records in the current attempt confirm truncation,
no patch, verification, review or remote artifact exists, and the existing
output recovery allowance is unused. Current-policy tasks, stale evidence,
older-attempt records and used allowances do not qualify. Retry still requires
current source/confirmation evidence and matching task version, archives the
failed attempt, and never resets attempt numbers. Deployment alone does not
queue repairs, call providers or publish contributions. This is bounded
recovery, not a guarantee that every provider will complete a repair.

## Independent Review Correction (2026-10-26, Issue #59)

An independent rejection now returns its conclusion, risks, candidate and real
verification to the repair agent automatically. There are two review corrections
in addition to the existing two verification corrections, at most five candidates.
Every candidate is rebuilt from the immutable original, retested and independently
reviewed with a fresh mandatory evidence read. The repairer's exploration allowance
grows only with actual bounded corrections. Source, output, timeout, argument and
overall task budgets remain shared. Repeated candidates are rejected before tests.

Each review is persisted with its candidate revision and digest. Preparing another
candidate clears the current review while retaining all previous conclusions.
Only the latest passed candidate and its current approval authorize publication.

While repair automation and models are enabled, an old-policy review rejection may
automatically resume once if its owner confirmation, analysis/execution evidence,
complete verification, patch and source digest remain valid and no remote artifact
exists. The old attempt is archived and the policy allowance is durable. The new
workspace verifies the original source digest before any model call and cannot
resubmit that rejected patch unchanged. Paused, stale, current-policy or previously
recovered tasks do not receive another automatic allowance. This specific upgrade
recovery can invoke models and publish according to the existing automation setting.
