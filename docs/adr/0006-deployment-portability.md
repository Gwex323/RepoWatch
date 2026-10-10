# ADR 0006: Local Windows Deployment, Later Linux Migration

Date: 2026-10-09
Status: accepted for deployment sequence; implementation choices unresolved

## Context

The user wants to run the first version on the current host and migrate it
to a Linux server later. The current workspace is on Windows.

## Decision

The initial deployment target is the user's current Windows host.
The subsequent deployment target is a Linux server.

Portability between those environments is a design requirement. Docker was
subsequently selected for project execution under ADR 0009. The maintenance
system's agent framework was subsequently selected as the Python OpenAI Agents
SDK under ADR 0011. Its database and operating-system service manager remain
undecided.

The implementation language of the agent backend is Python.
Separately, the user has confirmed Python, Go, and TypeScript as target
repository languages for version one. Supporting those repositories must
be considered across the Windows and Linux deployment targets.

## Design Implications

- Keep host-specific paths, credentials, and runtime configuration separate
  from core analysis and contribution logic.
- Scheduling and worker startup need an operating-system-independent contract,
  even if deployment adapters differ.
- Define how repository registrations, finding history, pending work, and
  scheduling state survive migration without duplicate contributions.
- Treat credential provisioning as a deployment concern, not as source code.

## Open Questions

- The user has no model-provider restriction. Which models and adapters should
  be selected for Agents SDK, and what credentials must be provisioned?
- Is Docker installed and usable on the current host, and what configuration
  is needed for the selected project execution policy?
- How should scheduled work behave when the host is asleep or offline?
- Which state must be migrated, and where should it be stored?
- What resources will be available on the future Linux server?
