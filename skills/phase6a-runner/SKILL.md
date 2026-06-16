---
name: phase6a-runner
description: "Runs Phase 6A Local Demo Entry / CLI tasks from 109 through 111 and stops before Phase 6B."
version: "1.0.0"
---

# Skill: Phase 6A Local Demo Entry / CLI Runner

## Purpose

Run Phase 6A Local Demo Entry / CLI tasks automatically and stop after Task 111.

## Task sequence

- 109 Assistant CLI / Local Demo Entry
- 110 Demo Scenario Polish
- 111 Phase 6A Review

## Read first

```text
AGENTS.md
docs/116-phase6a-local-demo-entry-roadmap.md
tasks/README_PHASE6.md
```

## Execution rules

- Finish one task before starting the next.
- Respect each task Scope.
- Do not expand beyond the current task.
- Use apply_patch for manual edits.
- Do not retry without sandbox.
- Do not install dependencies from the network.
- Do not write API keys.
- Do not create secret `.env` files.
- Do not call real external Providers by default.
- Default execution must remain mock/local/offline.
- Do not build a complex frontend.
- Do not call real Providers.


## Allowed commands

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
ruff check
ruff format
mypy
```

## Stop condition

Stop after Task 111. Do not start the next phase automatically.

## Final response

After Task 111, summarize:

```text
Phase 6A Local Demo Entry / CLI complete.
- Tasks completed:
- Tests run:
- Remaining issues:
- Recommended next phase:
```
