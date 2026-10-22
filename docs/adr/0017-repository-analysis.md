# ADR 0017: Automatic Repository Analysis

Date: 2026-10-12
Status: implemented; live-provider acceptance requires owner model configuration

## Trigger And Evidence

Completed test or startup attempts automatically produce a repository-analysis
task, including failed commands with useful logs. Installation alone does not.
The background worker also discovers existing eligible results on upgrade.
The environment revision, configuration fingerprint and most recent setup,
test and start job identities form the deduplication key. Polling and service
restarts do not repeatedly analyze identical evidence. New execution evidence
supersedes waiting tasks and makes previous reports historical.

Analysis is independent of the target application's model credentials and
container uptime. It reads source and already recorded execution observations;
it does not require the owner to open the project first. It does not run new
model-generated commands, browser actions or test code in this increment.

An additive SQLite queue persists task state, source snapshot, input memory
references, role activations, native handoffs, findings and owner feedback.
Tasks wait visibly when the analysis model is disabled or missing. Saving and
enabling a model releases waiting tasks. A ten-minute deadline and 23-turn
limit bound each attempt. An expired running lease becomes interrupted and
requires an explicit retry, avoiding silent replay of billable calls.

## SDK Review

Four existing roles participate through native handoffs:
repository_analyst, problem_investigator, reproduction_assistant and
independent_reviewer. The final role returns structured findings. The other
two existing roles, repair_agent and pr_author, are not enabled for real
repositories by this increment. The fixture graph remains unchanged.

Tools only read captured source lines and execution records. They cannot
access credentials, execute programs, edit code or publish contributions.
No source code is executed on the host during analysis. Repository instructions,
logs and memory are untrusted input and cannot authorize actions.

Snapshots contain up to 100 eligible text files, 16,000 bytes per file and an
approximately 400,000-character total budget. Supported file types cover
Python, Go, TypeScript/JavaScript, common manifests and documentation. Hidden
paths, symlinks, dependencies, build/data directories, lockfiles and filenames
suggesting credentials are excluded. Coverage counts and truncation are
disclosed. This is bounded review, not exhaustive whole-repository analysis.

Each source read returns at most 120 lines; the run allows 40 source reads and
100,000 source characters, with line-number prefixes for accurate citations.
A finding must quote real captured text
inside an actually read line range. Both source and execution reads and all
three handoffs must occur before a report is accepted. Eight findings maximum
are accepted. Passing tests or an empty report never establishes bug absence.

### Stage Budgets (2026-10-13)

Each role has a separate exploration budget. Unused capacity is not borrowed
from downstream roles:

| Role | Exploration calls | Source reads | Source characters |
| --- | ---: | ---: | ---: |
| repository_analyst | 4 | 10 | 25,000 |
| problem_investigator | 7 | 16 | 40,000 |
| reproduction_assistant | 3 | 6 | 15,000 |
| independent_reviewer | 5 | 8 | 20,000 |

Each role also has one reserved closing call. A per-role SDK Model adapter
adds remaining budgets to system instructions on every call. Once any limit
is reached, it removes source/execution tools from the provider request and
selects the native handoff tool, or disables tool calls for the final reviewer.
The SDK still executes the model's real handoff; no synthetic handoff is
inserted. A provider that ignores the forced choice fails explicitly.
Tool handlers enforce read and character limits even for parallel tool bursts.
An agent that finishes early without the required handoff still fails review.
The overall deadline remains a hard cap; provider slowness may still end an
attempt before all roles run.

Each attempt records its start, model-call stages, budget boundaries and
credential-free failure details. Failures distinguish turn exhaustion,
handoff noncompliance, malformed output, provider rejection, timeouts and
explicit shutdown/disable. HTTP status, exception class, stage and elapsed
time are retained, but provider bodies and exception messages are not.
Retries preserve history and reset current progress. Old generic failures
cannot be retrospectively assigned a specific cause.

Six recent active repository memories at the exact revision may be supplied,
with content limited to 800 characters per entry. IDs and versions are saved
with the analysis snapshot. These unverified references never replace current
evidence or owner confirmation.

## Provider Configuration

Issues #47 and #55 add a saved provider catalog and five shared agent-role selections.
See [供应商与智能体模型](../model-providers.md) for the import format and routing contract.
Each role can retain its independent configuration or select a saved provider.
The original single-model API updates all five roles for compatibility. Each run
resolves and freezes its role models, keys and reasoning settings before opening
clients; subsequent configuration edits cannot redirect an in-flight role.
Memory evaluations compare configuration revisions and all role-model identities.

The owner configures endpoint, API mode, model and a dedicated API key in
the WebUI. Both Responses and Chat Completions adapters are available. The
provider must support tools and structured output; saving settings is not a
successful connectivity test. No model or provider is silently substituted.

Keys use the existing OS-vault-backed encryption service and are stored
encrypted separately from GitHub account tokens. API responses never return
the key; invalid request responses do not echo secret input. A version check
protects updates and changing endpoint requires a new key. Model credentials
are not injected into target containers or prompts. Only HTTPS or local HTTP
endpoints are accepted; redirects and ambient proxy configuration are disabled.
Transport-level retries and tracing exports are disabled. Each model call has
a 120-second wall-clock limit (and 120-second transport read timeout); connect,
write and pool waits are bounded separately. Only timed-out model requests
are retried, after two seconds, at most once per call and twice across an
entire analysis. Retries keep identical input and tool settings without replaying
completed stages or tools. They do not consume another exploration turn.
Authentication, tool-argument, rate-limit and other failures are not automatically
retried. Confirmed truncation and final-report format correction are the bounded
exceptions described below.
Cancellation and the ten-minute overall deadline interrupt both
requests and backoff; the task lease includes a thirty-second margin.
Retry events are visible in the workbench. A timed-out provider may already
have processed/billed the request, so this is bounded replay, not exactly-once
provider execution. The whole failed analysis is never silently restarted.

### Output Diagnostics (2026-10-14)

Provider response hooks record allowlisted finish reasons, output-token and
reasoning-token counts. Text, tool arguments, provider errors and arbitrary
metadata are never recorded. Chat Completions finish_reason=length and Responses
incomplete/max_output_tokens are treated as truncation even if partial text or
tool calls are present: no truncated result reaches the SDK tool executor.
An observed SDK adapter preserves this classification when the SDK rejects an
empty truncated completion first.

Confirmed truncation permits one retry of that request with twice its output
limit, capped at 12,000 tokens and two output retries across the entire run.
Stage-turn limits and the overall deadline are unchanged. This can increase
cost but does not rerun prior tools/stages. Unknown malformed tool output is not
automatically altered or replayed. Empty provider envelopes and invalid tool
JSON get distinct errors. Historical generic ModelBehaviorError records cannot
retroactively establish truncation or malformed arguments.

### Final Report Contract (2026-10-14)

A subsequent live attempt reached independent_reviewer and failed after 135
seconds. Its last completion ended normally with stop and 31 output tokens,
without truncation. The historical JSON error covers both syntax and schema
validation, and raw output was not retained; the exact malformed content cannot
be reconstructed.

The reviewer now receives the complete strict JSON Schema in system instructions,
including an explicit ban on Markdown fences, tool syntax and source-read
arguments as a final report. Final text is checked before returning it to the
SDK. One format correction is allowed across the entire attempt, using the
same conversation and schema, with exploration tools removed and tool choice
none. Earlier stages and tools are not replayed. The invalid response is not
inserted into the correction input, logged or persisted. Only an allowlisted
syntax/schema category and the correction event are recorded.

The correction shares existing timeouts, retry limits and the ten-minute
deadline; it does not reset stage counters. Refusals and tool calls remain
subject to SDK handling and closing-stage checks. A correction that tries to
call a tool is rejected. Corrected text still passes strict SDK validation,
source quote/range checks and all handoff checks before findings are committed.
JSON-object output alone is not a valid report.

For the exact glm-4.7 model on the official open.bigmodel.cn or api.z.ai
Chat Completions endpoints, a narrow adapter requests json_object instead of
json_schema on the wire. Strict local output_type validation is retained.
Other models, custom gateways and Responses keep their existing protocol.
The selected output protocol is visible in model status and analysis events.
This resolution does not rewrite saved configuration, change providers or
models, rotate keys, or trigger a live retry.

### Tool-History Finalization Follow-Up (2026-10-14)

The next owner-triggered attempt exhausted the format correction and failed in
independent_reviewer after 354 seconds. Both final completions ended normally
with 32 output tokens and invalid JSON. Raw production output remains
unavailable; its exact contents are not established.

Synthetic-only probes against the owner's configured glm-4.7 endpoint
reproduced a related failure: a long native tool history followed by a no-tools
finalization returned textual `<tool_call>` markup rather than a JSON report.
Simple requests, including a short tool history, returned valid JSON. Thus
changing response_format and repeating the same history is insufficient.
This is a cross-layer provider compatibility assumption and integration
coverage gap, not evidence of a Docker failure.

At reviewer budget closure, the model now receives a single serialized evidence
packet instead of native assistant/tool messages. It contains only actually
read source lines with original numbers/ranges, deduplicated across reads,
execution context, completed handoffs, and up to twelve prior assistant
observations (2,000 characters each). Reasoning and provider metadata are not
included; observation clipping is explicit. Source lines are not truncated or
expanded beyond verified reads. Existing source-read/character budgets bound
the packet. JSON is serialized structurally, never sliced after encoding.
The same packet is used for the one permitted format correction. If invalid
output occurs before budget closure, that correction switches to the packet
then. A generic StageModel without an evidence supplier wraps its existing
non-reasoning transcript as data instead.

This is still the same independent reviewer inside the native SDK run. It does
not add an agent, replay tools, reset budgets, execute textual tool requests,
fill missing report fields, or relax evidence validation. Invalid output is
not inserted in the retry request. Syntax diagnostics retain only numeric
length/position counts and fixed categories (including empty output and
textual tool markup); they do not retain raw text, quotes, reasoning, arguments
or exception messages. The WebUI names finalization and these diagnostics.

A synthetic evidence-packet request passed strict report-schema validation,
but one source quote failed exact matching. It was not an accepted repository
finding. A full-history correction probe also timed out under its diagnostic
45-second request limit. The change mitigates the reproduced protocol failure;
it does not establish production completion or model evidence quality. Live
repository retry remains owner-triggered, with all evidence and human gates.

### Reasoning And Time Budgets (2026-10-14)

A real review reached independent_reviewer in 228 seconds but its closing call
spent 4,999 of 5,000 output tokens on reasoning before truncation. A larger
output request then timed out, and its retry hit the 600-second hard deadline.
Raising the timeout alone cannot prevent this failure mode.

Model settings now include an owner-selectable reasoning effort. Auto requests
low effort for the exact glm-5.3 model name; other names retain provider defaults.
Existing settings without this field behave as auto, without rewriting keys
or changing providers. Default explicitly omits reasoning settings. Low, medium,
high and max are explicit choices; medium is rejected for glm-5.3. The SDK sends
these through its native ModelSettings reasoning configuration. The gateway's
actual compliance and the quality/latency tradeoff still require live validation.

### GLM-4.7 Finalization Latency (2026-10-14)

A subsequent owner-triggered run completed all three handoffs, but failed with
analysis_provider_timeout_exhausted after 573 seconds. The reviewer's initial
exploration request timed out at 120 seconds, then completed its retry after
about 114 seconds with 3,405 reasoning tokens out of 3,464 output tokens.
The evidence-packet finalization path ran, but both report requests hit 120
seconds without a completed response. Those two requests have no returned
usage, so their exact reasoning/token use is unknown. This is a separate
latency failure, not proof the earlier JSON error recurred or of a network fault.

Official BigModel thinking documentation states that GLM-4.7 defaults to
thinking, supports per-request thinking.type=disabled, and does not use the
newer-model reasoning_effort control. Official Z.AI documentation also
describes turn-level control. Sources:
https://docs.bigmodel.cn/cn/guide/capabilities/thinking.md and
https://docs.z.ai/guides/capabilities/thinking-mode.md.

For the existing exact official GLM-4.7 JSON Object profile, auto mode now
requests thinking.type=disabled only for a structured final report with no
tools or handoffs and tool_choice=none. Exploration and all native handoffs
retain their previous settings. An explicit reasoning choice, including
provider default, opts out. Explicit extra_body thinking settings are not
overwritten. Other models, custom endpoints and Responses are unchanged;
in particular this must not disable thinking on GLM-5.3.

The mode is derived from saved configuration without migrating the key,
configuration or version. Status and the WebUI show the saved report mode.
An allowlisted event records each request that actually adds the disabled
thinking parameter. This does not assert provider compliance or guarantee
completion. Strict report/source validation, independent review, human
reproduction, retry ceilings and the ten-minute deadline remain unchanged.

One approved, synthetic-only request using the production adapter returned in
9.7 seconds with 284 output tokens and zero reported reasoning tokens. Its
report passed both schema and exact source-evidence validation. It was not
inserted as a real finding. This establishes the narrow API path, not
production review latency, coverage or correctness.

Exploration closes on either the previous limits or elapsed role windows
(90/150/60/90 seconds respectively). With 240 seconds left in the ten-minute
run, all roles close exploration and perform their actual native handoff or
final review. This threshold is checked before every request, not an in-flight
preemption. Every request is capped by the remaining overall time as well as
the normal 120-second bound. Slow providers can still exceed the deadline.
Time-boundary and requested-effort events are visible. Evidence and all
handoff validation remain mandatory; early closure is not permission to invent
findings, bypass independent review, or claim exhaustive coverage.

Enabling the provider permits sending bounded source, execution logs and memory
to that service. Redaction covers common credential formats, but does not
guarantee detection of arbitrary secrets. Provider exceptions are classified
without persisting raw exception text.

### Exact Source Evidence Follow-Up (2026-10-14)

The next owner-triggered attempt returned a complete JSON report after 399
seconds, including a final normal-stop response with 2,909 output tokens and
zero reported reasoning tokens. Final local validation rejected its evidence.
The invalid report was not retained, so no specific path, quote or range can
be identified retrospectively. Line-prefix copying remains a hypothesis, not
an established cause of that production failure.

Inspection and regression tests expose an independent contract mismatch:
compacted evidence can contain adjacent source reads, while the validator
previously required an entire citation to fit inside one original read.
Validation now accepts the union of actually read lines, rejecting any gap.
Paths, range bounds, quote substrings and read coverage still must match the
immutable source snapshot. This does not make an unread source line eligible.

Finalization now supplies structured source_blocks entries with separate line
numbers and raw text, rather than embedding line numbers in the source text.
If a quote is invalid solely because every line carries the exact display
prefix for its declared full range, the prefix may be removed only when the
result passes all the same path/range/read/quote checks. Already valid literal
numeric prefixes remain intact. No fuzzy text matching, whitespace repair,
ellipsis expansion, path guessing, line relocation or invented quote filling
is allowed.

The independent reviewer validates evidence before returning final output to
the SDK. At most one evidence correction is allowed, sharing the existing
run deadline, timeout/output limits and stage turn; it does not replay tools
or handoffs. It receives only safe indexed error categories and the trusted
evidence packet, not the raw invalid report. Tools are disabled. A repeated
mismatch fails without saving findings; an empty correction must disclose
limitations rather than silently claiming all suspected findings disappeared.
One format correction and one evidence correction are separate ceilings
within the unchanged overall deadline.

The canonicalized report is serialized back through the strict SDK output
schema and checked again at persistence. Safe events contain only finding/
evidence indices and fixed reasons, or normalization counts. No raw invalid
quote, source text or arbitrary error message is persisted by diagnostics.
The WebUI shows those reasons and evidence correction state. These changes
address reproducible contract defects; real provider evidence quality and
successful completion of the owner's repository remain unverified.

## Owner Feedback And Limits

Findings start as unconfirmed system discoveries. Only authenticated, CSRF-
protected owner feedback can mark personal reproduction. Clicking the
"reproduced successfully" action explicitly confirms personal reproduction;
no additional checkbox or observed-result note is required. Notes are optional
for either outcome and are preserved when supplied. The API still requires
explicit personal confirmation for a successful outcome. Revision/configuration and
execution-evidence checks reject stale feedback; optimistic versions prevent
duplicate decisions. Feedback persists independently of the target container.

Confirmed findings are labeled as waiting for the future repair integration.
No real Issue ingestion, source repair, Issue/PR publication, automatic merge
or daily scheduling is enabled here. Existing-Issue fixture gates are unchanged.

The WebUI shows model-blocked work, current review role, evidence, coverage,
limitations, history, handoffs and feedback. Workbench notices link to the
appropriate model settings or repository analysis.

## Validation

Tests execute actual SDK tools and handoffs using a deterministic test model.
They cover source bounds, encrypted credentials, version checks, automatic
queueing/resumption, idempotency, stale evidence, interruption handling,
secret-free failure reporting, memory scope and owner/CSRF gates.
Browser tests cover model settings, secret-field clearing, report rendering,
mobile layout and personal reproduction feedback. No test model result is
inserted into the owner's real repository records.


### DeepSeek Official Chat Compatibility (2026-10-21, Issue #33)

The saved deepseek-flash Chat Completions configuration used high reasoning;
the latest attempt failed on the first repository_analyst request with HTTP
400. The persisted allowlisted event contains no rejected parameter, so it
does not identify the exact upstream error. Code and the official contract
establish an incompatible combination: mandatory reads use named tool_choice,
while DeepSeek enables thinking by default and rejects named/required tool
choices in that mode. Its Chat Completions response_format supports text and
json_object; server-side strict function schemas require a separate beta mode.

For HTTPS api.deepseek.com at the default port and root, /v1 or /beta base
paths, Chat Completions now uses a dedicated JSON Object adapter. All requests
explicitly disable thinking and omit reasoning_effort, including exploration,
mandatory reads, handoffs, diagnosis and repair. Keeping the whole run in one
mode avoids changing reasoning-history requirements midway through tool use.
This applies even to a saved explicit effort: status reports effective effort
as null, the WebUI explains the compatibility policy, and analysis records an
allowlisted provider_compatibility event. Saved settings, credentials and their
version are not migrated. Other hosts, paths, ports and Responses are unchanged.

The adapter copies function tools and SDK handoffs with strict=false on the
wire; it preserves their schemas, native handlers and original strict flags.
Final reports use json_object with the existing prompt schema and strict local
output/evidence validation. Mandatory reads, exact evidence checks, actual SDK
handoffs, bounded correction/retry and human confirmation gates remain intact.
Updating and restarting the backend applies the derived policy to existing
configuration; failed attempts require an explicit retry.

Request-body regression fixtures exercise mandatory execution/source reads,
all three forced handoffs, JSON report success and invalid-report rejection
for every saved effort, shared adapter selection for diagnosis/repair, and
non-mutation of settings/tools/credentials. They do not call a live provider
or establish production model quality or full end-to-end DeepSeek acceptance.

Official contracts:
https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
https://api-docs.deepseek.com/zh-cn/guides/tool_calls/

### Ark Coding GLM-5.3 Report Compatibility (2026-10-21, Issue #38)

The owner-triggered GLM-5.3 attempt completed all three native handoffs, then
failed on final-report correction. The initial text was invalid JSON; after
tools were removed, the adapter restored JSON Schema response_format and the
Coding Plan endpoint rejected that field with HTTP 400. Tool/schema separation
alone therefore did not cover report closure or correction.

Two synthetic live probes using the configured GLM-5.3 endpoint and high effort
confirmed that json_schema was rejected at response_format.type while json_object
returned valid JSON. This is evidence for that configured endpoint, not a claim
about every GLM service or the general Ark API.

For HTTPS ark.cn-beijing.volces.com at the default port, exact /api/coding/v3
base path (optional trailing slash), Chat Completions and glm-5.3/glm-5.3-flash,
reports now use the existing JSON Object adapter. Exploration keeps tools and
output-format constraints separate; report closure and correction both use
json_object. Saved/effective reasoning is preserved, including explicit high;
the GLM-4.7 fast-finalization policy must not disable thinking for these models.

Whenever the JSON Object adapter removes a wire schema, it also supplies that
schema in system instructions. Shared diagnosis and repair callers therefore
retain the model-visible report contract as well as strict local output_type
validation. Source quotes, report structure, genuine SDK handoffs, retry limits
and failure behavior remain enforced locally. No credentials/configuration
migration, automatic task retry or automatic PR merge is performed.

Request-body regressions cover targeted URL/model/API matching, exploration,
native handoffs, report closure, successful correction and persistent invalid
syntax/structure. Synthetic probes do not satisfy full repository-analysis
acceptance, and GLM success cannot close DeepSeek tracking Issue #33.
