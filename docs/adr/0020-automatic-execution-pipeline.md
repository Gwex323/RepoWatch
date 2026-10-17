# ADR 0020: Automatic repository execution pipeline

Date: 2026-10-16
Status: implemented, validated and exercised with explicit live-analysis consent

## Requirements

Download latest -> isolated setup/dependency reuse -> real test success -> service
start -> bounded HTTP access verification -> evidence-bound repository analysis.
No skipped/missing tests count as passing. No automatic start after failed tests.
Explicit test-only mode skips service/access rather than fabricating success.
Install-network consent, Docker isolation, confirmation and publication gates stay.

Saving only start/port must retain installed dependencies and current valid tests.
Changing tests invalidates only test/access results; changing language/install/network
invalidates setup/test/access. Analysis reports remain bound to the full config,
while eligible setup/test logs are matched by action-specific fingerprints.

## Design and safety

Persist pipeline rows and link execution jobs. Completing a step and scheduling its
successor is one SQLite transaction. Duplicate download requests reuse the active
pipeline. State/version/config/account binding rejects stale completions. Cancel
marks the pipeline first and revokes pending/running jobs; completion cannot enqueue
a successor. Uncertain interrupted execution is not silently replayed.

The public download action defaults to automatic execution, with an explicit UI
manual-download option. Existing internal Execution.create callers retain manual
semantics unless auto_run is passed. Existing environments are not auto-migrated.
An explicit continue endpoint can start from installed/passing-test evidence without
reinstalling/retesting. Owner configuration overrides are saved per repo/identity
and applied to later downloads; no credentials are inferred or added.

A waiting configuration/environment resumes only after validation and owner intent
remain current. Docker checks are outside write locks and use bounded backoff.
Service probes use loopback preview ports only, fixed GET paths, no credentials,
no redirects and bounded time/response metadata. Test-only mode is explicit.
Pipeline analysis is deferred until execution/access finishes; a disabled model
waits rather than calling a fallback model. Stop/cancel cannot be undone by a late
analysis completion. No automatic repair, Issue, PR or merge is newly authorized.

## Validation plan

Mock pipeline success/failure/cancellation/duplicates/restart/configuration tests;
retention and action-specific evidence tests; source-entry discovery without
executing code; isolated browser workflow; real Docker authored fixture only.
Full backend, lint, typecheck/build and browser regressions before rollout.
The live environment receives its known demonstration start configuration only
after testing, via approved application operations and with a database backup.


## Implementation details and safeguards

- `/api/repositories/{id}/environments` accepts strict `auto_run` (default true).
  Internal/manual clients can explicitly opt out. A strict per-environment continue
  endpoint and `continue_pipeline` configuration flag support atomic save/resume.
- A configuration-only save cannot automatically initiate new model analysis from
  older full-config execution records. An explicit pipeline or owner request can
  use still-valid action-specific evidence; source/configuration reports remain
  version-bound. This prevents a race between configuration save and service start.
- Python service entry discovery uses bounded AST parsing, not host imports.
  Only explicit owner overrides are persisted per repository and GitHub identity.
- The initial TCP socket is closed before HTTP verification. Keeping it open while
  probing HTTP deadlocked a single-threaded server; a loopback HTTP regression and
  the real Docker fixture now exercise this boundary.
- Pipeline-owned analysis has a unique trigger component. Cancellation and identity
  binding are checked during model execution, and late completion cannot overwrite
  cancelled state or insert findings. Execution/setup alone never confirms findings
  or authorizes a new Issue/PR.

## Final validation and rollout

- Full backend regression: 535 passed, ten opt-in Docker tests skipped.
- All eleven browser tests passed; the pipeline test was rerun after waiting for
  mobile menu animation before capturing screenshots. No horizontal overflow.
- The real Docker automatic-pipeline test passed for two authored Python revisions,
  including dependency reuse, real tests, service startup, HTTP access and native
  SDK analysis with a deterministic model. No repository source was sent out.
- TypeScript/Vite build, Ruff and whitespace checks passed.

The real fixture initially found a TCP/HTTP deadlock with a single-threaded
server. The fix releases the bare socket before issuing HTTP, rather than
increasing timeouts. A real loopback single-threaded server regression prevents
recurrence. The legacy diagnosis API regression now opts out of automatic mode
explicitly; the public default/manual opt-out is independently covered.

The personal workbench received a code-only rollout after an idle check, a
SQLite backup and log backup. Its existing successful installation/test evidence
and image were preserved while saving the demonstration start command. The
backend and frontend now match. No live execution pipeline was queued: pipeline
count remained zero, analysis count remained three and execution queues stayed
empty. The prior published and failed repair tasks were left alone.

Starting the live pipeline would disclose bounded private source and execution
records to the configured external model service. That step remains unexecuted
pending explicit owner confirmation of destination and payload. The QA instance
was stopped. No Issue/PR write or real model call occurred during this change.


## Explicitly authorized live run

The owner explicitly approved sending the bounded repository source and execution
records to the configured model destination. The destination/model were checked
against that consent before queuing work. The existing passing-test environment
continued at service startup; its pipeline execution ledger contains only start,
not setup or test. The demonstration project started successfully, HTTP access
returned 200 and the pipeline reached completed after the native multi-agent
analysis finished. The analysis produced zero new findings within its reviewed
scope; this is not an assurance that the entire project is defect-free.

Existing repair states and publication records were unchanged. No finding was
confirmed and no new Issue/PR was published. The remaining review limitations are
visible in the stored report, including unreviewed orchestration/frontend/SSE paths.
