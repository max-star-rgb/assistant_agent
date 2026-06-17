# Phase 7A Runtime Profile Review

Phase 7A establishes runtime configuration profiles without changing the default offline behavior.

## Completed Scope

- Added `RuntimeProfile` in `src/multimodal_agent/runtime_profile.py`.
- Added supported profiles:
  - `local_demo`
  - `offline_eval`
  - `provider_smoke`
  - `pilot`
- Added `MULTIMODAL_AGENT_RUNTIME_PROFILE`, defaulting to `local_demo`.
- Wired runtime profile loading into `ProviderConfig.from_env()`.
- Kept default CLI, API, Web Console, eval, and demo flow paths mock/local/offline.
- Blocked real/network Provider selectors in `local_demo` and `offline_eval`.
- Allowed explicit real/network Provider selectors only in `provider_smoke` and `pilot`.
- Preserved local-only provider selectors such as product `local_json` and price `local`.
- Added tests for default profile, unknown profile errors, provider config wiring, and safety boundaries.

## Safety Guarantees

- An API key alone does not enable a real Provider.
- Default `local_demo` does not call real Providers.
- `offline_eval` ignores real/network Provider selectors and stays deterministic.
- `provider_smoke` does not fall back to mock when an explicit real Provider is missing required configuration.
- Provider errors and smoke outputs must not include API keys, authorization headers, bearer tokens, full base64 payloads, or raw Provider responses.

## Verification

Task-level checks run during Phase 7A:

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
```

Task 127 acceptance:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Non-goals

Phase 7A did not:

- Add new real Providers.
- Call real Providers by default.
- Add authentication or user/session ownership.
- Implement pilot readiness checks.
- Change capability behavior, planner behavior, or intent quality.
- Build new Web Console features.

## Next Track

The next Phase 7 track is Phase 7B Real Provider Production Hardening.

Recommended next task:

```text
Task 128 Real Provider Config Validation
```
