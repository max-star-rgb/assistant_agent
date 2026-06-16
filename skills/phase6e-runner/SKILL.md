---
name: phase6e-runner
description: "Runs Phase 6E Documentation Consolidation / Release Review tasks from 121 through 123 and stops."
version: "1.0.0"
---

# Skill: Phase 6E Documentation Consolidation / Release Review Runner

## Purpose

Run Phase 6E Documentation Consolidation / Release Review tasks automatically and stop after Task 123.

## Task sequence

- 121 Documentation Consolidation
- 122 Release Checklist and Cleanup
- 123 Phase 6 Review

## Read first

```text
AGENTS.md
docs/120-phase6e-docs-release-roadmap.md
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
- Do not delete phase docs unless explicitly requested.
- Prefer archiving or linking.


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

Stop after Task 123. Do not start the next phase automatically.

## Final response

After Task 123, summarize:

```text
Phase 6E Documentation Consolidation / Release Review complete.
- Tasks completed:
- Tests run:
- Remaining issues:
- Recommended next phase:
```
