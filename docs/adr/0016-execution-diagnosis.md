# ADR 0016: Evidence-Based Execution Diagnosis

Date: 2026-10-12
Status: implemented

## Context

A dependency installation with networking disabled failed to resolve PyPI.
The final pip message reported no matching package version, obscuring the
network cause. The user requested diagnosis and remedies inside the workbench.

## Decision

Persist the revision and execution configuration when an operation is queued.
After setup, test or startup failure, save a bounded, redacted evidence snapshot
and a local report. Cancellation, interrupted operations and pending cleanup
do not produce actionable remediation. Backfill only the latest unchanged
failed environment when upgrading older records.

Local rules identify networking, runtime constraints, dependency conflicts,
filesystem permissions, missing build tools, npm lockfile mismatches and empty
test collection. Empty collection with pytest manifest evidence recommends
the declared runner and installing test dependencies without claiming a pass.
Unknown failures remain explicitly uncertain. An installation DNS failure
with networking disabled proposes enabling installation networking only.

An opt-in model path reuses three existing roles through native SDK handoffs:
repository analyst, reproduction assistant, independent reviewer. The last
agent returns structured advice. Both handoffs are mandatory and quoted
evidence must appear in the supplied log or dependency manifests.
No execution, credentials, approval or publication tools are provided.
Repository content and logs are untrusted data, not instructions.

The model receives the last 16,000 log characters, supported execution fields,
and up to 8,000 bytes each from requirements.txt, requirements-dev.txt,
pyproject.toml, pytest.ini, package.json and go.mod.
Symlinks and other files, including .env, are not read. Common token
formats and named secrets are redacted, including the configured diagnosis key.
This is best-effort redaction; opting in authorizes sending this bounded project
context to the configured provider.

Configuration uses dedicated REPOWATCH_DIAGNOSIS_* environment variables.
Default operation remains offline model fixtures plus local diagnosis rules.
Both Responses and Chat Completions model adapters are supported; provider
compatibility still depends on tool calls and structured-output support.
Tracing is disabled. Six turns, request timeouts and a 90-second overall
deadline bound diagnosis. A separate worker avoids blocking execution.
Interrupted calls retain local advice without automatic replay.

The WebUI labels the actual source of advice and its historical/current state.
An explicit apply action uses only the saved server-side patch, checks the
environment version and failure/configuration/revision identity, invalidates
installation and test results, and atomically queues installation. Repeated
or stale submissions fail. Recommendations are never represented as tested.

## Consequences

Diagnosis gives concrete remedies without equating installation failure with
a confirmed repository bug. No source repair, automatic Issue creation,
publication or approval bypass is added. The original six-role fixture graph
and saved human checkpoints remain unchanged.

Model settings currently require process environment configuration and restart;
there is no provider settings form. Native handoffs and fallback behavior are
tested using deterministic SDK models. A live provider call has not been
validated without a configured credential.
