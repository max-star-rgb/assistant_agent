# Phase 7B Real Provider Production Hardening Review

Phase 7B hardens opt-in real Provider paths while preserving mock/local/offline defaults.

## Completed Scope

- Added real Provider config validation in `src/multimodal_agent/services/provider_config_validation.py`.
- Added provider readiness checks and a standard smoke result contract in `src/multimodal_agent/services/provider_readiness.py`.
- Added redacted diagnostics and safety default summaries in `src/multimodal_agent/services/provider_diagnostics.py`.
- Added tests for:
  - default offline validation
  - missing config detection
  - `provider_smoke` readiness
  - smoke contract shape
  - diagnostic redaction
  - timeout/retry/fallback safety defaults

## Runtime Safety

- Default runtime profile remains `local_demo`.
- `offline_eval` remains offline.
- Real/network Providers still require:
  - `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke` or `pilot`
  - explicit Provider selector
  - required key/base URL/model settings for that provider
- API keys alone do not select real Providers.
- Missing real Provider config returns structured `provider_unconfigured` style issues.
- Diagnostics redact API keys, authorization headers, bearer tokens, raw response fields, full base64 payloads, and sensitive local paths.

## Manual Real API Smoke Guidance

Codex did not call real Providers during this phase.

To test a real Provider manually, use a local shell or untracked local env file. Example for Qwen vision:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="set this only in your local shell"
export QWEN_VISION_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export QWEN_VISION_MODEL="qwen-vl-plus"
python scripts/smoke_real_vision.py --image <local-image-path>
```

Expected success signs:

- output `provider` is `qwen`
- output does not contain `mock://vision/white-low-top-sneaker`
- output contains a structured `vision_result`
- no API key or authorization header appears in stdout/stderr

Expected setup failure signs:

- missing config exits clearly with `provider_unconfigured`
- no fallback to mock is reported as success

## Verification

Phase 7B verification:

```bash
python -m pytest
python scripts/check_env.py
python scripts/run_evals.py
git status --short
```

## Non-goals

Phase 7B did not:

- Add new real Provider families.
- Call real Providers by default.
- Implement cloud secret management.
- Add production billing.
- Add auth, user/session ownership, or Web Console product features.

## Next Track

The next track is Phase 7C Web Productization Baseline.

Recommended next task:

```text
Task 132 Web Console UX Baseline
```
