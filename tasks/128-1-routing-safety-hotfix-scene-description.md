# Task 128-1 Routing Safety Hotfix: Scene Description Must Not Trigger render_3d

## Goal

Fix rule routing and planning so scene-description prompts route to media understanding, not `render_3d`.

## Read first

- `AGENTS.md`
- `docs/125-phase7-production-readiness-roadmap.md`
- `docs/126-phase7a-runtime-configuration-profiles.md`
- `docs/127-1-routing-safety-hotfix-scene-description.md`
- `tasks/README_PHASE7.md`

## Scope

- Intent router / rule router
- Planner tool selection
- Capability validation if needed
- Routing and eval tests

## Requirements

- Do not modify Provider adapters.
- Do not call real APIs.
- Do not write API keys.
- Keep default runtime profile as `local_demo`.
- Keep default mock/local/offline behavior.
- Do not modify `tools/__init__.py`.
- Do not renumber existing Task 128-148 files.
- Do not start Task 128-2.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py --suite routing
```

## Stop condition

Stop after this hotfix. Report changed files, test results, and remaining routing risks.
