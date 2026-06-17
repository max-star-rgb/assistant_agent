# 127 Phase 7B Real Provider Production Hardening

## Goal

Make opt-in real Provider paths safer and more consistent for controlled pilot usage.

## Scope

- Normalize real Provider config validation.
- Add provider readiness checks.
- Standardize smoke result contracts.
- Add timeout / retry / cost defaults per Provider family.
- Add redacted provider diagnostic summaries.

## Runtime Profile Rules

- `local_demo`: no real Provider calls.
- `offline_eval`: no real Provider calls.
- `provider_smoke`: real Provider smoke allowed only with explicit config.
- `pilot`: real Provider allowed only after readiness checks.

## Out of Scope

- No default real Provider calls.
- No new Provider families unless already skeletonized.
- No committed real outputs.
- No cloud secrets manager.
- No production billing.

## Success Criteria

- Missing config fails clearly.
- Smoke output is standardized.
- Diagnostics are redacted.
- Default tests/evals/demo remain offline.
