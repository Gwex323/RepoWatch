# Milestone One Validation

Date: 2026-10-09

## Final Results

| Check | Result |
| --- | --- |
| Python tests | 18 passed |
| Ruff | All checks passed |
| TypeScript and production WebUI build | Passed |
| Playwright end-to-end tests | 2 passed |
| Desktop and mobile screenshots | Visually reviewed |
| Project editable installation | Passed |
| Clean local instance | Started on 127.0.0.1:8765; owner setup required |

The independent browser-QA server was stopped after verification. Its
database is separate from the user's clean instance.

## Implemented Contracts

- Six actual Python Agents SDK agents and five observed native handoffs in a
  complete controlled run.
- The SDK interrupts before the human reproduction tool can execute.
- No issue draft, patch draft, review artifact, or PR draft exists before
  authenticated personal reproduction confirmation.
- A saved SDK interruption can be restored in a new operating-system process.
- One finding's confirmation cannot unlock a different finding.
- Duplicate confirmation is idempotent; conflicting decisions are rejected.
- Negative evidence, stale source versions, corrupt snapshots, and unapproved
  tool execution cannot proceed to repair or publication.
- Runtime snapshots are JSON, kept in trusted backend storage, and not returned
  to the browser.
- Login, CSRF, origin, hostname, and repository URL validation are exercised.
- The Windows JavaScript MIME regression is covered without weakening
  content-type enforcement.

## Browser Coverage

Desktop at 1440 x 1080 and mobile at 390 x 844:

- First-owner setup/login.
- Starting a controlled sample and reaching the human checkpoint.
- Manually changing reproduction inputs and inspecting observed results.
- Positive confirmation and subsequent SDK continuation.
- Negative reproduction and absence of repair artifacts.
- Viewing all five handoffs and unsubmitted local drafts.
- Registering a GitHub URL while keeping external execution disabled.
- Mobile navigation and horizontal-overflow checks.

Screenshots are generated in `test-results/` by the browser tests and are
visually reviewed separately from automated assertions.

## Known Limits

- Model outputs and patch content are deterministic fixtures, not a live LLM.
- Sample verification runs trusted built-in functions, not downloaded code.
- Docker containers, public issues/PRs, and recurring schedules are not used.
- Linux migration and real provider compatibility have not yet been tested.
- This is a local-only owner login, not a reviewed internet-facing deployment.
- Direct dependencies and the npm lockfile are pinned; the Python transitive
  dependency graph is not yet locked for multi-platform deployment.
