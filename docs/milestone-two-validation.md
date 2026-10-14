# Milestone Two Validation

Date: 2026-10-12
Branch: `feature/repository-execution`

## Results

- Ruff: passed.
- Backend suite including diagnosis and pytest discovery: 116 passed,
  3 opt-in Docker tests skipped by default.
- Opt-in real Docker suite: all 3 passed separately.
- TypeScript checking and Vite production build: passed.
- Playwright including diagnosis/apply flow: all 7 passed.
- Desktop and 390-pixel mobile screenshots inspected; no horizontal overflow.
- Git whitespace check: passed.
- Labeled test containers, volumes and networks: none left after validation.
  Downloaded official language images are retained.

## Real Container Coverage

The local Windows Docker Desktop Linux engine was started for this validation.
Tests used authored fixtures, not the owner's registered repositories.

Python, Go and TypeScript each completed source staging, environment setup,
testing, HTTP startup through the isolated preview, stop, another startup,
another stop, and resource cleanup. Container tests checked non-root execution
and absence of a GitHub token and Docker socket. Python also checked that the
root filesystem cannot be written.

Real validation caught and corrected Docker local-log rotation settings,
virtual-environment ownership, Go's need to execute compiled temporary files,
and the inability to publish ports directly from an internal Docker network.
The final preview uses a separate trusted forwarding container without a
project volume, with IP forwarding disabled.

## Boundaries

GitHub identity and fixed-revision archive downloads were tested with mock HTTP
responses. No personal PAT was used in test fixtures or browser interception.
The browser environment test uses synthetic execution responses; the separate
container suite exercises actual runtime operations.

Existing tests still cover the SDK human checkpoint, existing-Issue automatic
validation, native handoffs, memory isolation, account binding and CSRF.
Environment results do not unlock those gates.

Live provider diagnosis remains unvalidated without credentials. Native SDK
diagnosis handoffs were exercised using a deterministic test model; tests cover
evidence checking, model-failure fallback, stale advice, configuration snapshots,
explicit atomic apply/reinstall, legacy backfill and CSRF. Diagnosis screenshots
were inspected at desktop and 390-pixel mobile widths.

Following the owner's request to fix the failed installation, the latest
registered-repository failure was backfilled with local diagnosis. Applying its
saved install-network recommendation queued a real Docker installation, which
completed successfully and left the environment ready.

A subsequent test attempt exposed incorrect unittest selection for a pytest
repository, producing zero collected tests. Discovery now reads pytest.ini
and requirements-dev.txt, and setup installs the declared development dependencies.
After updating the registered environment and reinstalling in Docker, its
actual pytest suite passed: 17 tests in 1.11 seconds. No source changes were
made to that target repository. Startup validation is recorded below.
Eight additional backend cases cover runner selection and empty-test diagnosis.

## Registered Project Startup

The owner subsequently requested project startup. The retained revision provides
a FastAPI application at backend.main:app and serves its existing frontend
directory; its separate web directory is not needed for this preview.
The saved start command enables DEMO_MODE and runs the virtual-environment
Python with uvicorn on 0.0.0.0:8000. Setup and all 17 project tests passed again
after saving this configuration.

The project started through the existing isolated preview. Browser checks
verified HTTP 200, the page and input controls, local conversation creation,
conversation persistence after reload, and no uncaught JavaScript errors.
Desktop and 390-pixel mobile screenshots were inspected; no horizontal
document overflow was found. Only the conversation created by this check was
deleted afterward.

The health endpoint reports an unconfigured model credential. DEMO_MODE mocks
external tools but does not replace model calls in this repository. AI chat,
live maps and external tool integrations remain unvalidated; the existing
project network restrictions were retained. The preview stops automatically
after 30 minutes and can be restarted from the workbench.

General live model analysis, real-Issue intake, automatic repairs, GitHub publication,
daily scheduling, Linux-host acceptance and aggregate disk quotas remain outside
this increment. See ADR 0015 for runtime policies and ADR 0016 for optional diagnosis.
