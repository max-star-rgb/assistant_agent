# Image Generation ProviderSpec Registry Addendum

## Problem

Image generation provider metadata is still duplicated across config loading, adapter selection, validation, smoke scripts, and docs/tests.

## Target

Image generation provider metadata is represented by `ProviderSpec`, including:

- provider name
- provider selection env
- API key env when needed
- base URL env when needed
- model env and default when needed
- adapter kind
- required configuration

## Boundary

This task does not implement real image generation provider calls. Existing real-provider entries remain optional skeletons or manually-triggered smoke paths. Default tests and demo flows must continue using offline mock behavior.

## Expected Providers

- `mock`
- `openai`
- `qwen`
- `comfyui`
- `local`

`openai` and `qwen` require API keys. `comfyui` and `local` require base URLs. Missing required configuration must produce `provider_unconfigured`.

## Tests

- Default image generation stays mock/offline.
- Explicit real/local provider config validation reads from spec.
- Image generation smoke script uses spec-backed supported provider and missing config checks.
- Existing adapter behavior remains unchanged unless explicitly configured.
