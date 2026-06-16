# Architecture

The project is an intent-driven assistant agent.

## Runtime

Default runtime:

```text
AgentGraphRuntime
```

It uses LangGraph to execute the agent flow:

```text
request
  -> load memory
  -> detect intent
  -> plan/select tools
  -> execute tools
  -> compose response
  -> save memory
```

## Core Boundaries

- Agent: intent routing, planning, orchestration, response composition.
- Tools: stable capability boundary and structured results.
- Adapters: mock/local/optional real Provider implementations.
- Memory: local store and retrieval context.
- API: FastAPI wrapper for local demo and debugging.
- MCP/Skills: packaging layer over existing runtime/tool registry.

## Default Providers

Default Provider selectors remain mock/local. Real Providers are opt-in and documented in `docs/provider-setup.md`.

## Observability

Each run exposes:

- `run_id`
- `trace_id`
- `tool_calls`
- `errors`
- run summary
- trace summary

See `docs/observability-local.md`.
