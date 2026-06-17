# Vision ProviderSpec Registry Addendum

## Problem

Vision provider metadata is still spread across `ProviderConfig`, adapter factory code, validation, smoke scripts, and tests. Adding or adjusting an OpenAI-compatible Vision provider should not require editing each location independently.

## Target

`ProviderSpec` is the source of truth for Vision provider metadata:

- provider name
- provider selection env
- API key env
- base URL env and default
- model env and default
- adapter kind
- required configuration
- placeholder base URLs that must not count as configured

## Boundary

This task only centralizes Vision provider metadata and wiring. It does not add a new real Vision provider, call real APIs, write API keys, or change default mock behavior.

## Expected Providers

- `mock`
- `openai`
- `qwen`
- `seed`

`openai`, `qwen`, and `seed` remain explicitly selected real-provider smoke paths. Missing configuration must return `provider_unconfigured`; it must not fall back to mock after explicit selection.

## Tests

- Default config stays mock/offline.
- Provider smoke profile can select `qwen` Vision through spec.
- `seed` default placeholder base URL is treated as missing.
- Vision adapter factory reads resolved provider spec.
- Vision smoke script reads supported provider and missing configuration rules from spec.
