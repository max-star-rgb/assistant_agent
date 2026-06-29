# Agent Communication Routing

Last updated: 2026-06-29

This document is the current canonical entry for multi-agent instance routing, agent-to-agent communication, and A2A-style protocol adapter boundaries. Update it whenever agent directory/gateway behavior, agent communication services, `delegate_to_agent` tools, cross-instance sessions, A2A routes, JSON-RPC transport, or related safety policy changes.

Current status: opt-in local gateway/delegation boundary and inbound A2A JSON-RPC adapter implemented. The repository still keeps the existing `/agent/run`, CLI, eval, and Web demo paths on one default `AgentGraphRuntime` and does not register delegation in the default `ToolRegistry`. It now has protocol-neutral schemas, an `AgentDirectory`, `LocalAgentTransport`, `AgentCommunicationService`, a local multi-runtime factory, an opt-in `delegate_to_agent` tool, an `AgentGateway` service, a separate `POST /agents/run` API for same-process multi-agent routing, and inbound `/.well-known/agent-card.json` plus `/a2a/rpc` routes. It does not yet implement outbound A2A JSON-RPC, remote agent calls, or LLM target-agent selection.

Current stage boundary:

```text
Current stage implements a local same-process AgentGateway, not a full OpenClaw chat/network fabric.
Default product entrypoints still call agent.default through existing `/agent/run`, CLI, eval, and Web demo paths.
`/agents/run` is the explicit multi-agent gateway entrypoint.
`/.well-known/agent-card.json` and `/a2a/rpc` expose an inbound A2A-compatible JSON-RPC adapter over the local gateway.
delegate_to_agent exists only as an explicit registry-level opt-in local tool, enabled for the gateway controller runtime.
Any implementation must not change default single-agent behavior.
```

## Scope

Agent communication covers optional collaboration among multiple agent runtime instances. It is separate from:

- Memory service: long-term user/project memory, owned by `MemoryManager`.
- Context engineering: prompt/context pack construction, conversation history, observation compaction, and budget reporting.
- Provider adapters: real or mock LLM/image/video/product/provider integrations.
- MCP tools: tool exposure boundary. MCP is not the agent-to-agent communication model.

The intended design is OpenClaw-like at the runtime level: gateway, directory, session/task routing, allowlist, loop limits, and isolated agent instances. A2A JSON-RPC can be one transport adapter for interoperability, not the core internal model.

## Default Baseline

The default path remains single-instance and unchanged:

```text
User / CLI / API / Web UI
  -> FastAPI routes or local runner
  -> AgentGraphRuntime / assistant loop
  -> AssistantDecision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> Tool / Adapter / MemoryManager
```

When only `agent.default` exists, routing should behave like the current implementation. Do not introduce gateway behavior that changes default mock/local/offline runs.

## Target Multi-Agent Shape

Optional multi-instance routing should be layered as:

```text
User / API / Web UI
  -> AgentGateway
  -> AgentDirectory
  -> target AgentGraphRuntime
  -> existing assistant loop and tool boundary
```

Agent-to-agent delegation should remain a tool-governed action:

```text
Agent A assistant decision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> delegate_to_agent tool
  -> AgentCommunicationService
  -> AgentTransport
  -> Agent B
```

This preserves the existing safety boundary. Assistant nodes must not directly call another runtime, HTTP endpoint, A2A client, queue, or remote SDK.

## Core Concepts

Planned internal contracts:

- `AgentInstance`: one configured runtime identity such as `agent.default`, `agent.vision`, or `agent.product`.
- `AgentDirectory`: registry of agent IDs, capabilities, endpoint metadata, enabled transports, and allowlist policy.
- `AgentGateway`: entrypoint that selects the initial target agent for inbound user/API requests.
- `AgentCommunicationService`: application service used by tools or API routes to send messages/tasks to another agent.
- `AgentTransport`: transport interface. Implementations may include local in-process calls, A2A JSON-RPC over HTTP, or a future queue.
- `AgentMessage`: internal message envelope independent of any external protocol.
- `AgentTask`: internal task/request envelope with target, session, correlation, timeout, and budget fields.
- `AgentArtifact`: structured output reference or summary returned from a delegated task.
- `AgentSessionRef`: user/session/parent-run/correlation identity for isolation and traceability.

These contracts should live in `schemas/` and `services/` before any transport-specific details leak into `agent/` or tool implementations.

Implemented files:

| module | status | responsibility |
| --- | --- | --- |
| `src/multimodal_agent/schemas/agent_communication.py` | implemented | Internal message, task, artifact, session ref, route request/result contracts. |
| `src/multimodal_agent/schemas/a2a.py` | implemented | Minimal inbound A2A JSON-RPC request/response constants and schema. |
| `src/multimodal_agent/schemas/agent_gateway.py` | implemented | External `/agents/run` request contract with explicit target and collaboration mode fields. |
| `src/multimodal_agent/services/agent_directory.py` | implemented | Default `agent.default` identity, capability metadata, enablement, and simple routing. |
| `src/multimodal_agent/services/agent_transports.py` | implemented | `LocalAgentTransport` for same-process runtime calls and normalized results. |
| `src/multimodal_agent/services/agent_communication.py` | implemented | Service boundary and local multi-runtime factory for routing an `AgentTask` through an enabled transport. |
| `src/multimodal_agent/services/agent_gateway.py` | implemented | Local `AgentGateway` that manages `agent.default` plus `agent.worker`, supports `single` and `controller_delegate`, and returns `AgentRunResponse`. |
| `src/multimodal_agent/services/a2a_adapter.py` | implemented | Inbound A2A adapter that maps agent card and JSON-RPC `SendMessage` requests to/from `AgentGateway`. |
| `src/multimodal_agent/tools/agent_delegation_tool.py` | implemented | Opt-in `delegate_to_agent` tool backed by `AgentCommunicationService`. |
| `src/multimodal_agent/api/routes_agent.py` | implemented | Existing `/agent/run` plus separate `/agents/run` gateway endpoint sharing trial access rules. |
| `src/multimodal_agent/api/routes_a2a.py` | implemented | Inbound A2A-compatible agent card and JSON-RPC endpoint over local gateway. |
| `tests/test_agent_communication_routing.py` | implemented | Offline tests for default routing, local transport, disabled/unknown agents, depth limits, opt-in registration, and local delegation tool behavior. |
| `tests/test_agent_gateway.py` | implemented | Offline tests for gateway routing, controller delegation registry shape, structured unknown-agent failure, and single-agent compatibility. |
| `tests/test_api_a2a.py` | implemented | Offline API tests for agent card, JSON-RPC `SendMessage`, method errors, invalid params, and failed task mapping. |

## Protocol Boundary

A2A JSON-RPC should be treated as an adapter:

```text
A2A Agent Card / Message / Task / Artifact
  <-> internal AgentDirectory / AgentMessage / AgentTask / AgentArtifact
```

Rules:

- Do not make `AgentGraphRuntime`, assistant nodes, or core tool execution depend on A2A schema classes.
- Do not require A2A for local in-process multi-agent tests.
- Implement `LocalAgentTransport` first for deterministic offline behavior.
- Inbound A2A routes must be thin protocol adapters over `AgentGateway` or `AgentCommunicationService`; they must not directly call a provider, memory store, or another runtime.
- Add `A2AJsonRpcTransport` only behind an `AgentTransport` interface.
- Remote network transport must be explicit opt-in and allowlisted.
- A2A failure must return structured errors; it must not silently fall back to local/mock success.

## Routing Rules

- Default single-agent routing is `agent.default` and must remain compatible with current CLI/API/demo/eval behavior.
- `POST /agent/run` remains the default single-agent HTTP endpoint and must not use `AgentGateway`.
- `POST /agents/run` is the explicit local multi-agent HTTP endpoint. It accepts `target_agent_id`, optional `capability`, and `collaboration_mode`.
- `GET /.well-known/agent-card.json` exposes a local A2A agent card. It advertises the local `/a2a/rpc` JSON-RPC endpoint and enabled local agent skills.
- `POST /a2a/rpc` supports inbound A2A JSON-RPC `SendMessage` and the legacy-compatible `message/send` alias. It maps text and local media references into `AgentGatewayRunRequest`, then maps `AgentRunResponse` into an A2A task-like result.
- A2A metadata may carry `user_id`, `session_id`, `target_agent_id`, `capability`, and `collaboration_mode`. Missing user/session fields use local defaults and still pass through the same trial-access gate.
- `collaboration_mode="single"` directly runs the resolved target agent. If no target or capability is provided, the target is `agent.default`.
- `collaboration_mode="controller_delegate"` enters the controller path when no explicit target is supplied. The controller runtime uses the `agent.default` identity with `delegate_to_agent` registered. The normal single-mode `agent.default` runtime and worker runtimes do not register that tool by default.
- If `target_agent_id` is supplied, it remains the explicit initial route even when `collaboration_mode` is set.
- `delegate_to_agent` is not registered by `create_default_registry()` by default.
- Register `delegate_to_agent` only by passing `enable_agent_delegation=True` and an `AgentCommunicationService` to `create_default_registry(...)`.
- Use `create_local_agent_communication_service({...})` to build a same-process multi-runtime service for tests or explicit local experiments.
- The current opt-in is code/registry-level or the explicit `/agents/run` gateway. There is no default runtime environment variable that exposes delegation in normal `/agent/run` API/CLI runs.
- Multi-agent routing must be explicit by `agent_id`, capability match, or a configured routing table. Avoid hidden LLM-only target selection until deterministic policy and tests exist.
- A target agent must be enabled in `AgentDirectory`; unknown or disabled agents return structured errors.
- Outbound delegation must use a `delegate_to_agent` style tool through `ActionValidator` and `ToolExecutor`.
- Cross-agent calls must carry `user_id`, `session_id`, parent `run_id`, trace/correlation ID, timeout, and loop/depth metadata.
- User/session isolation applies across agent boundaries. One agent must not read another user's memory or session state through delegation.
- Loop control is mandatory. Track delegation depth, repeated target pairs, and ping-pong limits.
- Tool/provider budgets must include delegated work or carry a separate child budget linked to the parent run.
- Remote agents are external capability surfaces. Do not enable them because a URL, API key, or agent card exists.
- Trace and API output must redact secrets, raw provider responses, inline media/base64 payloads, and remote raw responses.

## Ownership

Module ownership:

| module | responsibility |
| --- | --- |
| `src/multimodal_agent/schemas/agent_communication.py` | Internal message, task, artifact, session ref, route request/result contracts. |
| `src/multimodal_agent/schemas/a2a.py` | Inbound A2A JSON-RPC protocol constants and public request/response wrapper schemas. |
| `src/multimodal_agent/schemas/agent_gateway.py` | External gateway request contract for `/agents/run`. |
| `src/multimodal_agent/services/agent_directory.py` | Agent registry, capability metadata, enablement, future allowlist, and routing table. |
| `src/multimodal_agent/services/agent_gateway.py` | Gateway service that selects the initial local runtime and augments `AgentRunResponse` with gateway metadata. |
| `src/multimodal_agent/services/a2a_adapter.py` | Protocol adapter between inbound A2A agent card/JSON-RPC payloads and internal gateway requests/responses. |
| `src/multimodal_agent/services/agent_communication.py` | Service boundary for sending messages/tasks through transports. |
| `src/multimodal_agent/services/agent_transports.py` | `LocalAgentTransport`, future `A2AJsonRpcTransport`, and transport result normalization. |
| `src/multimodal_agent/tools/agent_delegation_tool.py` | Agent-callable delegation tool registered in `ToolRegistry` only when enabled with an `AgentCommunicationService`. |
| `src/multimodal_agent/api/routes_agent.py` | HTTP interface for `/agent/run` and the separate `/agents/run` gateway route. |
| `src/multimodal_agent/api/routes_a2a.py` | Inbound A2A-compatible agent card and local JSON-RPC endpoint. |
| `tests/test_agent_communication_*.py` | Offline deterministic tests for directory, routing, transport, delegation, and safety policy. |
| `tests/test_agent_gateway.py` | Offline deterministic tests for gateway behavior. |
| `tests/test_api_a2a.py` | Offline deterministic tests for inbound A2A API behavior. |

If file names change during implementation, keep the same ownership boundaries and update this table.

## Local Multi-Instance Example

The supported local shape is explicit and same-process:

```python
from multimodal_agent.schemas.agent_communication import DEFAULT_AGENT_ID
from multimodal_agent.services.agent_communication import create_local_agent_communication_service
from multimodal_agent.tools.registry import create_default_registry

service = create_local_agent_communication_service(
    {
        DEFAULT_AGENT_ID: default_runtime,
        "agent.worker": worker_runtime,
    }
)
registry = create_default_registry(
    enable_agent_delegation=True,
    agent_communication_service=service,
)
```

This makes `delegate_to_agent` visible only in that explicit registry. It does not change the default API/CLI/Web demo registry, does not create an `AgentGateway`, and does not use A2A or network transport.

## Local Gateway API

The explicit HTTP gateway is:

```text
POST /agents/run
```

It reuses `AgentRunResponse` and adds `agent_gateway` metadata under `data` and `runtime_info`. The first version supports:

- `collaboration_mode="single"`: route directly to `target_agent_id`, capability match, or `agent.default`.
- `collaboration_mode="controller_delegate"`: route to the `agent.default` controller when no explicit target is supplied; this controller registry includes `delegate_to_agent`.
- `target_agent_id="agent.worker"`: explicit direct route to the local worker runtime.

The existing `POST /agent/run` endpoint does not use this gateway.

## Inbound A2A API

The local A2A-compatible discovery and JSON-RPC endpoints are:

```text
GET  /.well-known/agent-card.json
POST /a2a/rpc
```

Supported JSON-RPC methods:

- `SendMessage`: current A2A JSON-RPC message send method.
- `message/send`: compatibility alias routed to the same adapter.

The adapter extracts text from A2A message parts, maps image/video/audio file references when MIME types are available, reads local routing fields from metadata, and runs the request through `AgentGateway`. A failed agent run is returned as an A2A task with `status.state="failed"`; malformed JSON-RPC or unsupported methods return JSON-RPC error objects.

## A2A-Compatible MVP

Implementation order:

1. Add internal schemas and `AgentDirectory`. Done.
2. Add `LocalAgentTransport` and same-process multi-runtime tests. Done.
3. Add `AgentCommunicationService`. Done.
4. Add an opt-in `delegate_to_agent` tool. Done for local transport.
4a. Add a local multi-runtime factory for `agent.default -> agent.worker` style tests. Done.
4b. Add local `AgentGateway` and separate `/agents/run` API. Done.
5. Add inbound A2A-compatible agent card and JSON-RPC `SendMessage` route. Done.
6. Add outbound `A2AJsonRpcTransport` with allowlist, timeouts, and structured errors.

The inbound MVP exposes this repository as an agent without changing default `/agent/run` behavior. The outbound MVP should only call allowlisted local or explicitly configured remote agents.

## Update Rules

- Read this document before designing or changing agent instance routing, agent-to-agent communication, or A2A adapters.
- Keep `AGENTS.md` pointing to this file as the rule-routing entry.
- Keep `docs/DOCS_INDEX.md` synchronized when this document changes status or scope.
- If implementation affects architecture layering, also read and update `docs/architecture-layers.md`.
- If delegation affects prompt/context content, also read and update `docs/CONTEXT_ENGINEERING_STATUS.md`.
- If delegation reads or writes memory, also read and update `docs/memory-service-architecture.md`.
- Any implementation change should include offline tests first. Real remote-agent or network smoke tests must be explicit opt-in.

## Validation

For the current internal-boundary state:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_routing.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_gateway.py tests/test_api_agent_graph_runtime.py tests/test_api_a2a.py tests/unit/test_tool_registry.py
```

For broader behavior changes, run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_*.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
