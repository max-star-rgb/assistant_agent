# Task 128-3A Vision ProviderSpec Registry

## Goal

Move Vision provider metadata into the centralized ProviderSpec registry and make config, validation, factory, and smoke script read from it.

## Read first

- `AGENTS.md`
- `docs/125-phase7-production-readiness-roadmap.md`
- `docs/126-phase7a-runtime-configuration-profiles.md`
- `docs/127-2-provider-spec-registry-addendum.md`
- `docs/127-3a-vision-provider-spec-registry.md`
- `tasks/README_PHASE7.md`

## Scope

- Add Vision ProviderSpec entries.
- Resolve `ProviderConfig` Vision fields from spec.
- Keep legacy explicit `ProviderConfig(...)` construction compatible.
- Update Vision adapter selection and provider config validation to use spec.
- Update Vision smoke configuration checks to use spec.
- Add or update tests.

## Requirements

- Default path remains `mock`.
- No real Provider calls in tests.
- No API keys in code, docs, tests, or outputs.
- Missing explicit real-provider config returns structured unconfigured behavior, not mock fallback.

## Acceptance

- `python -m pytest` passes offline.
- Vision provider spec tests cover supported providers and missing config.
- Existing Vision smoke/factory tests pass.

## Stop condition

Stop after tests pass and report the next task.
