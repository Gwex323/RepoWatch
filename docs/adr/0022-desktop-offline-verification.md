# ADR 0022: Desktop project discovery and partial offline verification

Date: 2026-10-16 (Asia/Shanghai)
Status: implemented, regression-validated, and deployed as an unapproved personal proposal

## Scope

The cached Gwex323/gateway-model-assistant revision
9870a74369a81c53f9f4edb384598b240a16cbc0 is a Windows/Tkinter desktop project.
It declares no third-party runtime packages and has requirements-build.txt only for
packaging. Previously, absence of standard runtime manifests left every command
blank and stopped the pipeline at execution_config_required. Treating app.py as a
web server, installing PyInstaller, or running it on the host would be incorrect.

The first supported desktop profile is Python/Tkinter with standard-library/local
imports and unittest tests. It provides partial, isolated Linux offline verification,
not Windows GUI acceptance. No X server, Windows container switching, host Python
execution, GUI startup or real client configuration access is introduced.

## Static discovery contract

Only parse AST; never import downloaded modules. Follow local imports, relative
imports, package initialization and cycles. Literal __import__/import_module calls
are inspected; unknown dynamic imports, unresolved third-party imports, GUI/native
imports, stdlib shadowing and ambiguous local/test module names block automatic test
selection. GUI/runtime dependencies are described, not silently installed.

Scan at most 256 Python files, 256,000 bytes per file and 2 MiB total. Respect excluded
and hidden directories; reject symlink files and do not walk linked directories.
Malformed/unbounded sources remain manual. Candidate unittest modules must be under
tests/ with safe module paths. Invalid paths and selection limits are disclosed as
excluded. Select at most 32 modules and keep generated commands below 4,000 characters.

Generate an isolated venv setup and explicit module list, not repository-wide test
discovery that would import GUI tests. Build-only requirements are not runtime
requirements. Profile data records candidate/excluded modules and fixed unverified
Windows GUI/native/client-integration categories. These are static candidates, not
proof every selected test will execute or pass.

## Approval and persistence

The generated config proposes run_mode=test_only and no start command. It is NOT
execution authorization. First automatic setup waits for desktop_confirmation_required.
Saving config requires an owner-authenticated, CSRF-protected explicit
 desktop_offline_confirmed=true with test_only mode and a nonempty setup command.
The form requires acknowledgement of untested Windows features. All manual actions
and the execution worker enforce desktop approval. Desktop start is rejected even
if an owner enters a start string; this feature cannot produce a desktop preview URL.

Save the explicit approved offline mode per repository/account for later downloads,
including when it equals the inferred default. Do not reuse a desktop approval as a
web-project override, or inherit old web commands as desktop approval. Regenerate
candidate commands for each new revision unless the owner explicitly saved a custom
command. Existing custom commands are never overwritten by recognition.

POST /api/environments/{env_id}/configuration/detect handles old cached downloads:
owner/CSRF/version protected, only for uninstalled empty-command configs. It saves
an unapproved proposal and leaves the pipeline waiting; it queues no setup/model
job. First confirmation remains an owner decision. No GET-triggered migration.

Bind report fingerprints and test/start evidence to the project kind. Existing
same-revision environment reuse additionally checks profile kind and prior desktop
approval. Existing immutable pre-test dependency-cache policy is unchanged.

## Actual result and coverage

For desktop tests, retain unittest total, skipped and executed counts plus canonical
partial scope in test_result.desktop_scope. Zero, all-skipped or unrecognized test
execution cannot be promoted from exit zero to success (code 5); automatic analysis
is not scheduled. Custom commands are labeled custom and do not acquire generated
candidate coverage. The count parser currently recognizes unittest summaries, not
arbitrary frameworks. Exit/status/log summaries are evidence, not adversarial-code
attestation or proof all application behavior is correct.

A passing desktop pipeline goes setup -> selected tests -> source analysis. It does
not start a service or probe HTTP. Pipeline completion is labeled offline completion,
not full app acceptance. Analysis context includes the profile and actual scope; the
report receives execution_scope from the saved context, not a model assertion.
Execution and analysis UIs always disclose Windows GUI/native/client exclusions.
No model result changes original human confirmations or authorizes publication.

## Validation

Mock regressions cover declaration-only discovery, transitive/relative/package
imports, unknown/dynamic dependencies, build-only files, ambiguous/shadowed names,
symlinks/budgets, explicit approval, manual/worker guards, scope counts, all-skipped
failure, preferences, cached proposals and authenticated API constraints.

Two real isolated Docker validations passed before the interrupted session:
- Trusted fixture: one actual unit test, no GUI/HTTP and deterministic local review.
- Cached project: six candidate modules, 111 cases reported, 109 executed and passed,
  two skipped; eleven GUI/Windows-dependent modules were excluded entirely.

Both used network-disabled containers with no host credentials/client directories,
and deterministic local SDK models (not paid live analysis). They wrote temporary
QA databases, not personal test results. Initial QA dependency volumes were removed
under their distinct namespaces. A subsequent fixture-only rerun used the updated
finally cleanup; its final process result was lost on interruption and is not counted
as another pass. Docker was unavailable after resume, so no new real-container
validation or daemon-side post-interruption cleanup is claimed at that point.


## Final resume validation and rollout

After interruption, the former tool sessions were unavailable and both local servers
were offline. A fresh final regression ran against the current code: 624 backend
tests passed, twelve optional Docker cases skipped (378.03 seconds); all thirteen
browser tests passed (2.2 minutes). Result logs were saved under data/logs/. The
new desktop desktop/mobile page was visually checked; GUI/native exclusions and
partial report warnings remain explicit. Build, Ruff and whitespace checks passed.
No additional live Docker or paid-provider validation was claimed on resume.

The personal database/logs were backed up at
 data/backups/desktop-offline-rollout-20261016-170726. With no executable tasks pending,
only the existing empty-command desktop download was explicitly re-detected:
env_fcf4645bfb3245b8 version 3 -> 4. Its config now contains the six-module candidate
command, eleven exclusions, test_only proposal and desktop_approved=false. Its
pipeline_6e99b837e6a14b92 remains waiting_configuration, with the precise
 desktop_confirmation_required reason instead of the old generic command message.
Installed remains zero, test_result remains null and its sole job is still download.
No QA test result, approval, job or model report was copied into the personal ledger.

The personal workbench was restored on port 8765 and updated assets returned HTTP
200. Integrity/FK checks passed and all 27 protected table projections matched the
backup, excluding only the deliberately changed target environment/pipeline rows.
Other environments, credentials, preferences, findings, repairs and model-call
history were unchanged. Owner password/session configuration was retained. The
isolated QA server on port 8797 was identity-checked and stopped.

Docker was unavailable during resume. It was not started automatically. The owner
must start Docker Desktop and explicitly confirm the offline scope before the
personal pipeline executes. This is intentionally not full Windows acceptance.
