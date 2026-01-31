# Agent workspace: services/governance-agent

This folder contains agent-facing context, tasks, workflows, and planning artifacts for this submodule.

## Current State
Talos owner agent that governs policy and operational invariants. Domain logic has been migrated into a standalone Python project.

## Expected State
Production-grade policy enforcement with strong tests, pinned dependencies, and CI gates. Fail-closed on misconfiguration.

## Behavior
Evaluates and enforces governance decisions, manages session and state stores, and provides owner-level controls and automation.

## How to work here
- Run/tests:
- Local dev:
- CI notes:

## Interfaces and dependencies
- Owned APIs/contracts:
- Depends on:
- Data stores/events (if any):

## Global context
See `.agent/context.md` for monorepo-wide invariants and architecture.
