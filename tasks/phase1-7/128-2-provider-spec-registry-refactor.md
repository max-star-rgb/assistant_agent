# Task 128-2 Provider Spec Registry Refactor

## Goal

Centralize OpenAI-compatible chat Provider metadata so new chat Providers can be added through a ProviderSpec registry entry with minimal duplicated code.

## Read first

- `AGENTS.md`
- `docs/125-phase7-production-readiness-roadmap.md`
- `docs/126-phase7a-runtime-configuration-profiles.md`
- `docs/127-phase7b-real-provider-production-hardening.md`
- `docs/127-2-provider-spec-registry-addendum.md`
- `tasks/README_PHASE7.md`

## Scope

- Chat ProviderSpec / ProviderRegistry
- `ProviderConfig.from_env()` chat provider loading
- `create_chat_adapter()` chat provider selection
- provider config validation for direct_chat
- direct chat smoke script provider metadata
- `.env.example` / provider setup docs
- focused tests

## Requirements

- Do not call real Providers by default.
- Do not execute real DeepSeek/OpenAI/Qwen API calls.
- Do not write API keys.
- Do not create `.env` or `.env.local`.
- Keep default pytest/eval/demo offline.
- Do not modify `tools/__init__.py`.
- Do not renumber existing Task 128-148 files.
- Do not start original Task 128.
- Prefer `apply_patch`.

## Acceptance

```bash
python -m pytest
python -m pytest tests/test_direct_chat_adapter.py tests/unit/test_provider_config.py tests/test_provider_config_validation.py tests/test_provider_readiness.py tests/test_text_capability_smoke_scripts.py
```

## Stop condition

Stop after this refactor. Report changes, test results, and how original Task 128 should continue.
