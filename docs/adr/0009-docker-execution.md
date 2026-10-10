# ADR 0009: Local Docker Project Execution

Date: 2026-10-09
Status: accepted execution approach; host readiness and policy unresolved

## Context

The system downloads and runs third-party repositories locally. The user
accepted separate Docker containers for project dependency installation,
execution, and testing, with restricted directory mounts and permissions.

## Decision

Use Docker on the execution host to run projects in separate container
environments. This applies initially on the current Windows host and must
remain compatible with the planned Linux server deployment.

The user personally reproduces findings against the project's running
environment and confirms results in the WebUI, as required by ADR 0007.
Containerization does not remove or replace that human checkpoint.

This decision selects project execution, not the deployment packaging of
the WebUI, scheduler, or agent orchestrator.

The user subsequently decided that project containers should not stay
running while waiting for personal reproduction. ADR 0010 describes that
boundary and a proposed on-demand restart and feedback flow.

## Design Constraints

- Directory mounts and execution permissions must be restricted by policy.
- No unrestricted host access is implied by selecting Docker.
- Docker selection alone is not a complete security specification.
- Repository access credentials and model-provider credentials need explicit
  handling separate from project runtime requirements.

## Open Questions

- Is Docker installed and usable on the current Windows host?
- Which runtime images and versions support the initial repository languages?
- What CPU, memory, disk, process, and execution-time limits apply?
- What network access and ports are permitted?
- Which host paths may be mounted, and with what permissions?
- How does the user access web, CLI, and other project interfaces to reproduce
  suspected bugs?
- What data should be retained while reproduction environments are stopped,
  and how long should that data be kept?
- How are environments stopped, resumed, rebuilt, and cleaned up?

## Scope Of This Record

This records a design decision only. It does not assert that Docker is
installed, change host configuration, or start any container.
