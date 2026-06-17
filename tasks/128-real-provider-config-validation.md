# Task 128 Real Provider Config Validation

## Goal

Implement the Phase 7 task: **Real Provider Config Validation**.

## Read first

- `docs/127-phase7b-real-provider-production-hardening.md`

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


## Acceptance

```bash
python -m pytest
```

## Stop condition

Complete this task and then continue only if the active runner skill explicitly says to proceed.
