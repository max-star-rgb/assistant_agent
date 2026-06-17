# Task 128-3B Image Generation ProviderSpec Registry

## Goal

Move image generation provider metadata into the centralized ProviderSpec registry and make config, validation, adapter selection, and smoke script read from it.

## Read first

- `AGENTS.md`
- `docs/125-phase7-production-readiness-roadmap.md`
- `docs/126-phase7a-runtime-configuration-profiles.md`
- `docs/127-2-provider-spec-registry-addendum.md`
- `docs/127-3b-image-generation-provider-spec-registry.md`
- `tasks/README_PHASE7.md`

## Scope

- Add image generation ProviderSpec entries.
- Resolve `ProviderConfig` image generation fields from spec.
- Keep legacy explicit `ProviderConfig(...)` construction compatible.
- Update image generation adapter selection and provider config validation to use spec.
- Update image generation smoke configuration checks to use spec.
- Add or update tests.

## Requirements

- Default path remains `mock`.
- No real Provider calls in tests.
- No API keys in code, docs, tests, or outputs.
- Explicit configured non-mock providers must not silently fall back to mock when required config is missing.

## Acceptance

- `python -m pytest` passes offline.
- Image generation provider spec tests cover supported providers and missing config.
- Existing image generation smoke/adapter tests pass.

## Stop condition

Stop after tests pass and report the next task.
