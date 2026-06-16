---
name: phase6c-runner
description: "Runs Phase 6C Real Provider Opt-in Demo tasks from 115 through 117 and stops before Phase 6D."
version: "1.0.0"
---

# Skill: Phase 6C Real Provider Opt-in Demo Runner

## Purpose

Run Phase 6C Real Provider Opt-in Demo tasks automatically and stop after Task 117.

## Task sequence

- 115 Real Provider Opt-in Runbooks
- 116 Real Provider Smoke Matrix
- 117 Phase 6C Review

## Read first

```text
AGENTS.md
docs/118-phase6c-real-provider-opt-in-roadmap.md
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
- Do not call real Providers.
- Do not write real API keys.
- Only document opt-in smoke paths.


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

Stop after Task 117. Do not start the next phase automatically.

## Final response

After Task 117, summarize:

```text
Phase 6C Real Provider Opt-in Demo complete.
- Tasks completed:
- Tests run:
- Remaining issues:
- Recommended next phase:
```
