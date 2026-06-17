# Task 139 Phase 7D Review

## Goal

Implement the Phase 7 task: **Phase 7D Review**.

## Read first

- `docs/129-phase7d-auth-user-session-boundary.md`

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
docs/137-phase7d-auth-session-review.md
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
