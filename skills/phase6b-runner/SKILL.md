---
name: phase6b-runner
description: "Runs Phase 6B FastAPI Demo & Simple Web Console tasks from 112 through 114 and stops before Phase 6C."
version: "1.0.0"
---

# Skill: Phase 6B FastAPI Demo & Simple Web Console Runner

## Purpose

Run Phase 6B FastAPI Demo & Simple Web Console tasks automatically and stop after Task 114.

## Task sequence

- 112 FastAPI Demo Contract Stabilization
- 113 Simple Web Console
- 114 Phase 6B Review

## Read first

```text
AGENTS.md
docs/117-phase6b-api-web-console-roadmap.md
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
- Do not implement login or production permissions.
- Keep the web console minimal.


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

Stop after Task 114. Do not start the next phase automatically.

## Final response

After Task 114, summarize:

```text
Phase 6B FastAPI Demo & Simple Web Console complete.
- Tasks completed:
- Tests run:
- Remaining issues:
- Recommended next phase:
```
