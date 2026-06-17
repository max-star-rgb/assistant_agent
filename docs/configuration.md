# Configuration

The default configuration is mock/local/offline. No API key is required for local CLI, API, Web Console, evals, or tests.

## Core Defaults

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo
MULTIMODAL_AGENT_VISION_PROVIDER=mock
MULTIMODAL_AGENT_CHAT_PROVIDER=mock
MULTIMODAL_AGENT_IMAGE_PROVIDER=mock
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock
MULTIMODAL_AGENT_PRICE_PROVIDER=mock
MULTIMODAL_AGENT_RENDER_PROVIDER=mock
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
MULTIMODAL_AGENT_INTENT_ROUTER=rule
RUN_INTEGRATION_TESTS=0
```

## Runtime Profiles

`MULTIMODAL_AGENT_RUNTIME_PROFILE` controls which provider selectors are allowed to take effect.

| Profile | Purpose | Real/network Provider selectors |
| --- | --- | --- |
| `local_demo` | Default CLI, API, Web Console, and demo flows | ignored; mock/local defaults remain active |
| `offline_eval` | Deterministic eval and regression checks | ignored; mock/local defaults remain active |
| `provider_smoke` | Manual real Provider smoke checks | allowed only when the specific Provider selector and required config are explicit |
| `pilot` | Future controlled pilot usage | allowed only with explicit Provider config and later readiness checks |

Default behavior must stay:

```text
local_demo -> mock/local/offline
offline_eval -> mock/local/offline
```

Setting an API key alone does not switch any capability to a real Provider. A real Provider selector such as `MULTIMODAL_AGENT_VISION_PROVIDER=qwen` only takes effect under `provider_smoke` or `pilot`.

## Local Memory

Memory defaults to in-memory behavior unless configured otherwise by existing memory settings.

Useful local settings:

```text
MULTIMODAL_AGENT_MEMORY_BACKEND=memory
MULTIMODAL_AGENT_MEMORY_BACKEND=jsonl
MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/memories.jsonl
```

Do not commit `.local/`, `.data/`, local memory files, traces, logs, or real user data.

## Real Provider Opt-in

Real Provider setup is documented separately:

```text
docs/provider-setup.md
docs/real-provider-smoke-runbook.md
docs/real-provider-smoke-matrix.md
```

Set real Provider variables only in your local shell or an untracked local env file. Do not put real API keys into `.env.example`, docs, tests, or source code.

Manual real Provider smoke requires both a runtime profile and a Provider selector:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="set this only in your local shell"
python scripts/smoke_real_vision.py --image <local-image-path>
```

If the runtime profile is omitted, default `local_demo` protects the normal app path from real/network Provider selectors.

## Docker Compose Defaults

`docker-compose.yml` pins all Provider selectors to mock/default values. To opt in to real Providers, override values locally and do not commit those overrides.

## Config Safety Rules

- Default tests must stay offline.
- Default evals must stay offline.
- Default demo flows must stay offline.
- Default Web Console must stay offline.
- Real Provider selectors require `provider_smoke` or `pilot`.
- Missing real Provider configuration should produce a clear setup error instead of falling back to mock and pretending success.
