# Task 135 Phase 7C Review

## Goal

Implement the Phase 7 task: **Phase 7C Review**.

## Read first

- `docs/128-phase7c-web-productization-baseline.md`

## Scope

Stay within this task only. Do not jump to later Phase 7 tracks.

## Requirements

- Keep default runtime profile as `local_demo`.
- Keep default tests/evals/demo offline.
- Do not write API keys.
- Do not call real Providers by default.
- Do not commit real user data, real media, generated artifacts, rendered artifacts, or raw Provider responses.
- Prefer `apply_patch` for source, test, and documentation edits.
- Preserve existing mock/local behavior.

## Review Output

Generate:

```text
docs/136-phase7c-web-productization-review.md
```


## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

Complete this task and then continue only if the active runner skill explicitly says to proceed.
