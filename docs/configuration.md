# Configuration

The default configuration is mock/local/offline. No API key is required for local CLI, API, Web Console, evals, or tests.

## Core Defaults

```text
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

## Docker Compose Defaults

`docker-compose.yml` pins all Provider selectors to mock/default values. To opt in to real Providers, override values locally and do not commit those overrides.

## Config Safety Rules

- Default tests must stay offline.
- Default evals must stay offline.
- Default demo flows must stay offline.
- Default Web Console must stay offline.
- Missing real Provider configuration should produce a clear setup error instead of silently using real external services.
