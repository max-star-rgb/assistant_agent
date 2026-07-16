# assistant_agent

`assistant_agent` is a local-first multimodal autonomous tool-calling Agent. It uses a LangGraph/ReAct assistant loop, governed tool execution, provider adapters, memory services, API/demo/eval surfaces, and optional realtime Gateway entry layers.

## Start Here

- Coding-agent rules and repository boundaries: [AGENTS.md](AGENTS.md)
- Gateway and realtime lifecycle: `docs/gateway-architecture.md`
- Runtime and provider event streaming: `docs/runtime-event-stream-architecture.md`
- Tool calling governance: `docs/tool-calling-architecture.md`
- Observability and trace harness: `docs/observability-harness.md`
- Local memory service architecture: `docs/memory-service-architecture.md`
- External Memory Service interface: `docs/memory_server_api_spec.md`
- Context engineering status: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Durable structured execution is documented across the Gateway, tool-calling, and context authority documents above; its task API is under `/tasks/{task_id}` and is disabled by default.
- Multi-agent routing: `docs/agent-communication-routing.md`
- Media-Agent WebSocket contract: `docs/media-agent-service-websocket.md`

Memory operation runbooks (not authority):

- `docs/development/memory-dual-core-operator-runbook.md`
- `docs/development/memory-sqlite-operator-runbook.md`
- `docs/development/memory-framework-bakeoff-runbook.md`

Pilot and realtime operation runbooks:

- `docs/development/agent-pilot-operator-runbook.md`
- `docs/development/realtime-runtime-operator-runbook.md`

Interview training material lives under `docs/interview/` and is separate from normal development routing.

## Local Environment

The Python package is `assistant_agent` under `src/assistant_agent/`. The local conda environment remains `hello_agent`.

Default local runs use mock/local/offline providers，不会因为本地存在 key 自动启用真实调用。Real external providers are opt-in only through explicit runtime profiles such as `provider_smoke` or `pilot`, with keys supplied by local environment/config outside the repository. API key 只用于显式 opt-in 的真实 Provider smoke/pilot。

Basic checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Full offline validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## Documentation Policy

`AGENTS.md` is the coding-agent entrypoint. README is only a human navigation page. Current architecture decisions belong in the focused authority docs listed above. `docs/development/` is not a default starting point; it should contain only retained operational runbooks or explicitly named execution material.
