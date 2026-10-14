# ADR 0015: Fixed-Revision Repository Environments

Date: 2026-10-12
Status: implemented

## Boundary

Milestone two adds authenticated source download and local Linux Docker
execution. It does not connect live models, discover real findings, accept
real Issues, publish contributions, or change existing origin-dependent gates.
An installation failure, failing test command or listening server never grants
reproduction confirmation, repair permission or publication permission.

The environment is an independent durable resource with its own operations,
not a fixture SDK run. Future analysis must explicitly bind findings to its
repository, exact commit, configuration and evidence.

## Download And Retention

- Intake is still a validated `https://github.com/owner/repository` URL.
- A download captures the current verified account ID and credential version.
  A later account switch does not silently reassign queued downloads. Token
  replacement or disconnection invalidates a download using the old version.
- The worker verifies identity and repository access, resolves `HEAD` to a
  40-character commit SHA, then requests the archive for that SHA. Only the
  GitHub API receives the PAT; the codeload redirect receives no Authorization
  header. Redirect origin and repository/revision path are checked.
- This is a source snapshot, not a Git clone. Git metadata, history, submodules
  and LFS object materialization are not supported in this increment.
- Limits: 32 MiB compressed, 128 MiB of file content, 20,000 archive entries,
  and bounded decompressed tar metadata. Symlinks, hardlinks, special files,
  `.git` directories, parent traversal, duplicate case-folded paths and
  nonportable Windows names are rejected before extraction.
- Source is stored under `<database-stem>-workspaces/<environment-id>/source`
  next to the database. The source directory is never mounted into a project.
  Only validated file data is copied into the task's Docker volume.

## Runtime Policy

Only a local Docker context using a Unix socket or Windows named pipe and a
Linux engine is accepted. The application does not build repository Dockerfiles
or execute repository scripts on the host.

Runtime profiles use `python:3.13-slim-bookworm`, `golang:1.25-bookworm`, and
`node:24-bookworm-slim`. Missing images are pulled by the host adapter. The
selected image's content ID is saved for subsequent testing and restarts.
There is no browser-controlled image, mount, device or Docker flag.

All project containers use UID/GID 1000, a read-only root filesystem, all
capabilities dropped, no-new-privileges, Docker's default seccomp policy,
2 CPUs, 1 GiB memory with no additional swap, 256 processes, bounded file
descriptors, a 256 MiB per-file size limit, and a 256 MiB temporary filesystem.
Only the environment's named volume is attached. Code and package caches live
in `/workspace/project` and `/workspace/home`. No application database,
credential vault, Docker socket, host home, model key or GitHub token is passed
to project code.

Install commands default to `--network none`. The owner can enable network
access specifically for installation in the environment configuration. This
uses Docker's bridge network, including access to reachable network services;
it is not an allowlisted dependency proxy. Private dependency credentials are
not injected. Test commands always have no network.

Started projects use an internal network. A separate trusted Python TCP
forwarder has a published loopback port and joins that internal network; it has
no volume or credentials, targets only the recorded project's port, disables
IP forwarding, and bounds concurrent connections. The project has no external
network attachment or published port of its own.

The preview binds to `127.0.0.2` rather than the management hosts `127.0.0.1`
or `localhost`. Host-only management cookies must never be sent to the project.
The UI waits for both the upstream port and the host listener; a listening
socket indicates accessibility, not application correctness. Startup allows
up to 120 seconds for compilation and port binding.

Installation/test commands stop after 10 minutes. Started projects and preview
forwarders stop after 30 minutes, with container-side timeouts even if the
coordinator exits. There is no auto-restart policy. Containers are removed after
commands or explicit stop; source and volume data are retained for restart.
Normal coordinator shutdown also stops active previews.
Reinstalling recreates the volume from the retained source. Cleanup explicitly
removes that environment's source, volume and internal network after checking
its namespace labels. No global Docker prune is used.

There is no portable aggregate disk quota for named volumes in this increment.
Per-file, process and time limits are not an aggregate volume quota. Completed
environments need explicit cleanup; there is no automatic retention sweep.
Containers share the Docker engine kernel and are not a VM-level isolation
guarantee. Linux host deployment still requires separate acceptance testing.

## Discovery And UI

Root manifests choose one of Python, Go or Node/TypeScript. Python uses a
virtual environment and common project/requirements files; Go uses modules;
Node uses npm and declared test/start/dev scripts. Root-language ambiguity,
invalid manifests and unsupported package managers request configuration.
Python test discovery checks pytest.ini, structured pyproject pytest settings,
and pytest dependency declarations, including requirements-dev.txt. When pytest
is selected and requirements-dev.txt exists, installation includes that file.
Otherwise a conventional tests directory retains the unittest fallback.
Repository text is parsed only; it cannot select images or host operations.
Optional dependencies, system packages, monorepo directories and nonstandard
commands can require owner-provided command overrides. Additional OS packages,
multi-service Compose setups, private dependency credentials and arbitrary
images are not part of this increment.

The repository view exposes version downloads, Docker readiness, command/port
overrides, an installation-network checkbox, setup, test/CLI, start, stop and
cleanup. Logs are bounded and rendered as plain text. The environment view
retains failed operations; it does not create a bug finding from them.

## Persistence And Recovery

SQLite gains additive `environments` and `execution_jobs` tables. One active
operation per environment is enforced by a partial unique index. Repeated
download requests while a repository has an active operation reuse that
environment. Actions and config saves use optimistic versions. Config changes
invalidate installation and test results and require a new setup.

The embedded execution worker claims durable jobs and renews a lease. Expired
operations are cleaned up and marked interrupted, never automatically replayed
as if they were idempotent project scripts. Stop requests also cancel queued
or executing work. Failed cleanup is retained and retried. Use one application
coordinator per database; this is not a distributed execution service.

Real environment evidence does not enter the existing fixture Agent workflow.
The existing single-user confirmation interface remains independent of
container uptime. Automated discovery and revision-bound real-finding feedback
belong to milestone three.

## Validation

Backend tests cover authenticated revision download with mocked GitHub,
redirect credential separation, archive rejection, configuration invalidation,
durable operations, stop/recovery/cleanup, API auth and CSRF, and Docker policy.
Browser tests cover the environment workflow and desktop/mobile layouts with
synthetic responses. Opt-in real Docker tests use authored Python, Go and
TypeScript fixtures to exercise setup, test, HTTP access, stop, restart and
cleanup. No registered user repository is executed during these tests.
