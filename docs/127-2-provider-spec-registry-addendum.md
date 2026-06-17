# 127-2 Provider Spec Registry Addendum

## Current Problem

Provider metadata is currently spread across several places:

- `ProviderConfig`
- adapter factories
- smoke scripts
- provider config validation
- readiness checks
- `.env.example`
- provider setup docs
- tests

Adding an OpenAI-compatible chat Provider such as DeepSeek should not require copying the same provider metadata into every layer.

## Target Architecture

Introduce a small `ProviderSpec` / provider registry for provider metadata. The registry is the source of truth for supported Provider names and the environment variables required by each Provider.

For Phase 7B, this refactor starts with `direct_chat` because OpenAI-compatible chat Providers share the same adapter shape.

## OpenAI-compatible Chat Provider Spec

Each chat Provider spec should include:

- `name`
- `capability`
- `provider_env`
- `api_key_env`
- `base_url_env`
- `model_env`
- `default_base_url`
- `default_model`
- `adapter_kind`
- `requires_api_key`
- `requires_base_url`
- `requires_model`

Example:

```python
CHAT_PROVIDER_SPECS = {
    "deepseek": ProviderSpec(
        name="deepseek",
        capability="direct_chat",
        provider_env="MULTIMODAL_AGENT_CHAT_PROVIDER",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_CHAT_BASE_URL",
        model_env="DEEPSEEK_CHAT_MODEL",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        adapter_kind="openai_compatible",
    ),
}
```

## How Layers Use the Spec

`ProviderConfig.from_env()`:

- reads selected chat provider from `MULTIMODAL_AGENT_CHAT_PROVIDER`
- checks the selected provider against `CHAT_PROVIDER_SPECS`
- reads selected key/base URL/model from the selected spec
- keeps mock/local defaults when runtime profile does not allow real Providers

Adapter factory:

- uses the selected spec `adapter_kind`
- creates `HttpChatAdapter` for `openai_compatible`
- returns `UnconfiguredChatAdapter` when required config is missing

Validation/readiness:

- asks the spec which fields are required
- reports missing env names from the spec
- avoids duplicating provider-specific `if provider == ...` blocks

Smoke scripts:

- use the spec registry to list supported providers
- use spec-required env names for setup errors

Docs/env:

- list variable names that correspond to the spec
- do not include real secrets

## Standard Flow to Add a Chat Provider

1. Add one `ProviderSpec` entry to `CHAT_PROVIDER_SPECS`.
2. Add one focused test proving the spec is recognized.
3. Add placeholder variables to `.env.example` and docs if user-facing.
4. Do not add hardcoded adapter factory branches unless the provider uses a new adapter kind.

## Safety Boundary

This refactor does not:

- call real Providers by default
- execute DeepSeek/OpenAI/Qwen network calls
- write API keys
- create `.env` or `.env.local`
- change default `local_demo` or `offline_eval` behavior
- modify `tools/__init__.py`
