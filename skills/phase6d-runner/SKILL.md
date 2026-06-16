---
name: phase6d-runner
description: "Runs Phase 6D Local Deployment / Config / Observability tasks from 118 through 120 and stops before Phase 6E."
version: "1.0.0"
---

# Skill: Phase 6D Local Deployment / Config / Observability Runner

## Purpose

Run Phase 6D Local Deployment / Config / Observability tasks automatically and stop after Task 120.

## Task sequence

- 118 Local Deployment and Configuration
- 119 Healthcheck / Trace / Observability
- 120 Phase 6D Review

## Read first

```text
AGENTS.md
docs/119-phase6d-deployment-config-observability-roadmap.md
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
- Do not add Kubernetes.
- Do not build production monitoring stack.


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

Stop after Task 120. Do not start the next phase automatically.

## Final response

After Task 120, summarize:

```text
Phase 6D Local Deployment / Config / Observability complete.
- Tasks completed:
- Tests run:
- Remaining issues:
- Recommended next phase:
```
