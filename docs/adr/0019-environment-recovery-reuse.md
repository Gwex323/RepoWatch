# ADR 0019: Environment recovery and dependency reuse

Date: 2026-10-16
Status: implemented and validated

## Requirements and acceptance

1. A repair stopped before any model/workspace execution by unavailable Docker
   waits for the environment. Waiting checks use bounded backoff, do not spend
   model-attempt budget, and expose live readiness separately from historical
   errors. Recovery requires enabled repair/model settings, current owner
   confirmation/evidence and the same bound GitHub identity. Unconfirmed,
   stale, paused, completed and permission-denied tasks never auto-requeue.
2. Historical failures are not automatically resurrected on deployment. An
   explicit retry can credit a provably pre-model Docker failure once, preserving
   monotonically increasing attempt numbers and immutable attempt history.
3. Downloading identical source can point to an existing installed, idle
   same-repository/same-identity environment without destroying it. New revisions
   keep separate source/workspace/test and analysis evidence.
4. Successful setup can save a bounded, immutable dependency snapshot before
   tests/start mutate the environment. Compatible setup copies that snapshot
   read-only into a new private volume and copies the newly downloaded source;
   no test result is inherited. Repository, identity, image content ID, install
   command and dependency inputs bind compatibility. Unsafe/dynamic/custom
   installs fall back to normal setup, rather than guessing their inputs.
5. At most three snapshot slots per repository/identity. Missing, damaged,
   foreign or interrupted snapshots fall back safely; never delete foreign
   resources. Existing volumes remain intact.

## Design

Task context durably tracks bounded environment-wait backoff, source digest and
identity. Readiness runs before a claim, so new waits never increment attempts.
A one-time context credit on explicit historical recovery compensates only a
provably pre-model failure, retaining the existing model/recovery ceiling and
monotonic attempt history. New environment columns and a dependency snapshot
table are additive migrations. Wait checks run outside
SQLite write transactions; compare-and-swap and all freshness gates precede a
queue transition. Published patches do not require Docker to retry publication.

Historical preflight eligibility requires no artifacts, a direct coordinator
ExecutionError and no model/tool/test events in the current attempt. Its explicit
retry archives the old failure before crediting it. It is never migrated into a
waiting state automatically.

Reuse supports fixed ordinary requirements-only Python installs, go mod download
without local replacement/workspace inputs, and locked npm ci installs without
lifecycle hooks/local dependencies. Python packaging, lifecycle hooks and custom
commands retain full setup. Snapshot volumes are written only during capture,
then mounted read-only for restore; runtime tests never access the snapshot.
Same-version reuse verifies retained source bytes and excludes busy/deleted or
uninstalled environments. UI follows the reuse pointer and explains cache hits,
misses, waiting, historical failure and current engine state.

## Implementation and validation plan

- Add recovery state, audited refund and historical explicit recovery tests.
- Add pure compatibility fingerprints, bounded snapshot metadata and isolated
  Docker copy helpers; test cache isolation/fallback/cancellation and invalidation.
- Add exact-version environment reuse and new-revision evidence tests.
- Update browser states and regression cases, then run backend, lint, build and
  isolated browser tests. Optional container QA uses only bundled fixtures.
- During development do not start production work. Release requires an idle-queue
  check, database/log backups and an approved restart; never auto-retry old failures.


## Contracts learned from the failures

Root causes were an implicit assumption that environment preflight was a model
attempt, a cross-layer confusion between historical failure and live readiness,
and an all-or-nothing installation path without dependency provenance. Moving
preflight before a claim preserves the real model budget; a durable wait state
makes lifecycle recovery explicit rather than clearing errors optimistically.

No blocking Docker/filesystem I/O runs under the preflight write transaction.
Candidate version CAS, source-digest transfer into the claimed attempt and a
post-copy digest check close the recovery/copy race before any model call.
Restored waits carry the same account identity into the normal execution gate.

Snapshot capture happens while the setup job is still exclusive and before tests
or preview can mutate its home/dependencies. Only home and optional node_modules
are copied, never old project source or test evidence. Copy helpers use no
network, run as UID 1000 with the normal limits, and mount the origin read-only.
Both source and destination volume ownership are checked. Volume create may
return an existing foreign volume, so seed checks ownership before any copy.

This is not general build caching: editable installs, dynamic scripts, non-registry
npm resolution and unknown inputs use full setup. Snapshot slots remain bounded;
there is no LRU eviction or automatic disk-quota management in this increment.


## Final validation

- Backend: 513 passed, nine opt-in Docker cases skipped.
- Real Docker snapshot fixtures: Python, Go and TypeScript all passed on the
  final ownership-checking implementation; only authored fixtures were executed.
- Browser: all ten tests passed against an isolated QA instance. New states were
  visually inspected on desktop/mobile, with no horizontal overflow.
- TypeScript, Vite production build, Ruff and whitespace checks passed.
- No real model request, production repair retry or GitHub write was initiated.


## Approved rollout

After final validation, the personal instance was verified idle (no queued/running
repair, analysis, execution, diagnosis or SDK jobs). An approved restart retained
its original database and backed up both SQLite state and logs. The homepage
returned 200 with the new frontend, login setup remained intact, and additive
snapshot metadata was present with zero production snapshots.

Read-only post-restart checks confirmed the published repair stayed pr_created
at attempt 11. The old preflight failure stayed failed at attempt six, with
can_retry and docker_recovery_available true. It was not queued or executed.
The isolated QA service was stopped. No model request or GitHub write was made.
