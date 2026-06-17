# Task 125 Wire Runtime Profile into ProviderConfig

## Goal

Implement the Phase 7 task: **Wire Runtime Profile into ProviderConfig**.

## Read first

- `docs/125-phase7-production-readiness-roadmap.md`
- `docs/126-phase7a-runtime-configuration-profiles.md`

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
