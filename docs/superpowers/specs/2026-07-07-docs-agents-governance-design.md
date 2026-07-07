# Docs And AGENTS Governance Reset Design

## Goal

Reset the repository documentation system so future incremental development starts from a small, current, and durable set of sources.

The governance target is authority-first cleanup:

- `AGENTS.md` remains the single coding-agent entrypoint.
- `README.md` becomes a lightweight human navigation page.
- `docs/` keeps only current architecture authorities, necessary walkthroughs, current API/runbook material, and interview material.
- Clearly stale development plans, one-off execution artifacts, and broken-link documents are deleted directly instead of preserved indefinitely.

## Decisions

Use the strongest cleanup mode for clearly obsolete material:

- Delete obviously stale docs directly.
- Preserve only docs that still guide current development, operations, interviews, or project-owner understanding.
- Fix current docs when they contain stale links to removed material.
- Do not keep historical plans as active design input.

This is intentionally stricter than a move-only archive cleanup. The repository has already accumulated enough current authority documents, so keeping every old plan would continue to pollute future task routing.

## Document Roles

### AGENTS.md

`AGENTS.md` is the stable agent work entrypoint. It should contain only repository-wide rules that materially affect future coding-agent behavior:

- project position and runtime profile defaults
- architecture boundaries
- safety and provider rules
- directory ownership
- coding conventions
- documentation routing
- validation expectations

It should not carry long historical explanations, completed roadmap details, or module-specific implementation notes that belong in a focused authority doc.

### README.md

`README.md` is the human entrypoint. It should be lightweight but useful:

- what the project is
- where current development rules live
- how to run basic local checks
- which docs are current authorities
- which folders are not normal starting points

It should not duplicate architecture details from `AGENTS.md` or the specialized docs.

### Current Authority Docs

Keep a small set of top-level docs as current authority:

- `docs/gateway-architecture.md`
- `docs/tool-calling-architecture.md`
- `docs/observability-harness.md`
- `docs/memory-service-architecture.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/agent-communication-routing.md`

Each authority doc should state its scope near the top and avoid routing future work through stale `docs/development/**` plans except when explicitly needed for historical context or an active named runbook.

### Walkthrough Docs

Keep walkthrough docs only when they help the project owner or a new contributor understand a settled subsystem without becoming the source of truth:

- `docs/context-engineering-walkthrough.md`
- `docs/memory-module-walkthrough.md`
- `docs/agent-collaboration-walkthrough.md`

Each walkthrough should continue to point back to its authority doc and say it is not the development authority.

### API And Runbook Docs

Keep API and runbook docs only if they match current implementation or are still operationally useful.

Examples to review carefully:

- `docs/memory_server_api_spec.md`
- `docs/memory_server_software_implementation_design.md`
- `docs/development/memory-sqlite-operator-runbook.md`
- `docs/development/agent-pilot-operator-runbook.md`

If a document points to missing files such as `docs/CURRENT_DESIGN.md`, `docs/KNOWN_ISSUES.md`, or removed `docs/phase1-7/**` material, either update the references to current authorities or delete the document if its current value is low.

### Interview Docs

Keep `docs/interview/**` as a separate training corpus. It is not part of ordinary development routing unless the user asks for interview practice, scoring, standard answers, or interview documentation updates.

## Deletion Scope

Delete these categories directly:

- completed development plans now superseded by current authority docs
- one-off `docs/superpowers/**` specs and implementation plans after this governance effort is complete
- stale roadmap, phase, or MVP plans that are not the current execution plan
- docs whose only current function is to reference removed documents
- duplicate explanations that can be represented by a single authority doc plus an optional walkthrough

Do not delete:

- current authority docs
- current operational runbooks
- API contract docs that match current code
- interview training docs
- project-local skill files needed for agent routing

## Directory Shape

The target shape after cleanup should stay simple:

```text
AGENTS.md
README.md
docs/
  CONTEXT_ENGINEERING_STATUS.md
  agent-collaboration-walkthrough.md
  agent-communication-routing.md
  context-engineering-walkthrough.md
  gateway-architecture.md
  memory-module-walkthrough.md
  memory-service-architecture.md
  observability-harness.md
  tool-calling-architecture.md
  interview/
  development/
```

`docs/development/` should contain only active named plans or true operational runbooks that should remain outside the core authority docs. It should not be the default source for future implementation decisions.

## Update Rules

When architecture changes:

1. Update the relevant authority doc.
2. Update `AGENTS.md` only if routing, safety, directory ownership, or repository-wide coding rules changed.
3. Update `README.md` only if human navigation changed.
4. Update walkthrough docs only when their explanatory model becomes misleading.
5. Do not create a new docs file unless it has a durable role that existing docs cannot cover.

When deleting docs:

1. Search references with `rg`.
2. Remove or replace all links to deleted files.
3. Prefer current authority docs as replacement targets.
4. Run `git diff --check`.

## Validation

Governance cleanup is accepted when:

- `AGENTS.md` and `README.md` agree on entrypoint responsibilities.
- No remaining doc points to deleted files.
- Current authority docs still cover Gateway, tool calling, observability, memory, context engineering, and agent communication.
- `docs/development/**` is no longer treated as default design authority.
- `docs/superpowers/**` no longer remains as long-term project documentation after the governance task completes.
- `git diff --check` passes for changed docs.

## Non Goals

- Do not rewrite project architecture.
- Do not change runtime behavior.
- Do not rename the Python package, conda environment, or source tree.
- Do not convert README into a full marketing or public product page.
- Do not preserve every historical plan merely for record keeping.
