# Agent Communication Routing

Last updated: 2026-06-29

This document is the current canonical entry for multi-agent instance routing, agent-to-agent communication, and A2A-style protocol adapter boundaries. Update it whenever agent directory/gateway behavior, agent communication services, `delegate_to_agent` tools, cross-instance sessions, A2A routes, JSON-RPC transport, or related safety policy changes.

Current status: opt-in local gateway/delegation boundary, inbound A2A JSON-RPC adapter, and default-disabled outbound A2A JSON-RPC pilot transport implemented. The repository still keeps the existing `/agent/run`, CLI, eval, and Web demo paths on one default `AgentGraphRuntime` and does not register delegation in the default `ToolRegistry`. It now has protocol-neutral schemas, an `AgentDirectory`, deterministic `AgentRoutingPolicy`, `AgentDelegationPolicy`, `LocalAgentTransport`, `A2AJsonRpcTransport`, `AgentCommunicationService`, a local multi-runtime factory, an opt-in `delegate_to_agent` tool, an `AgentGateway` service, a separate `POST /agents/run` API for same-process multi-agent routing, inbound `/.well-known/agent-card.json` plus `/a2a/rpc` routes with public card filtering and JSON-RPC error taxonomy, and outbound allowlist/timeout/payload/protocol-error controls. It does not implement public remote agent fabric, automatic Agent Card discovery/enablement, or LLM target-agent selection.

Current stage boundary:

```text
Current stage implements a local same-process AgentGateway, not a full OpenClaw chat/network fabric.
Default product entrypoints still call agent.default through existing `/agent/run`, CLI, eval, and Web demo paths.
`/agents/run` is the explicit multi-agent gateway entrypoint.
`/.well-known/agent-card.json` and `/a2a/rpc` expose an inbound A2A-compatible JSON-RPC adapter over the local gateway.
delegate_to_agent exists only as an explicit registry-level opt-in local tool, enabled for the gateway controller runtime.
outbound A2A exists only as an explicitly configured transport on an enabled AgentDirectory entry.
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
- `AgentRoutingPolicy`: deterministic gateway route policy for explicit targets, routing tables, capability matches, controller fallback, and default fallback.
- `AgentGateway`: entrypoint that selects the initial target agent for inbound user/API requests.
- `AgentDelegationPolicy`: service-layer policy for source permission, allowed targets, depth, timeout, loop detection, budget metadata, and redacted audit events.
- `DelegationContextBuilder`: service-layer context boundary that filters parent history, memory context, raw payloads, and arbitrary metadata before dispatching a child task.
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
| `src/multimodal_agent/schemas/agent_communication.py` | implemented | Internal message, task, artifact, session ref, route request/result, and directory config contracts. |
| `src/multimodal_agent/schemas/a2a.py` | implemented | Inbound A2A JSON-RPC request/response, task result, artifact, message, and public agent-card schemas. |
| `src/multimodal_agent/schemas/agent_gateway.py` | implemented | External `/agents/run` request contract, collaboration mode enum, route-decision metadata, and delegated-task summaries. |
| `src/multimodal_agent/services/agent_directory.py` | implemented | Default `agent.default` identity, capability metadata, enablement, and directory config loading. |
| `src/multimodal_agent/services/agent_routing_policy.py` | implemented | Deterministic gateway routing policy, capability matching, routing-table overrides, and controller/default fallback selection. |
| `src/multimodal_agent/services/agent_delegation_policy.py` | implemented | Delegation permission, allowed-target, depth, timeout, ping-pong loop, budget metadata, redaction, and audit policy. |
| `src/multimodal_agent/services/agent_delegation_context.py` | implemented | Child-safe delegation context builder, memory scope filter, tool-result reference pruning, child budget metadata, and artifact summaries. |
| `src/multimodal_agent/services/agent_transports.py` | implemented | `LocalAgentTransport` for same-process runtime calls plus default-disabled `A2AJsonRpcTransport` with allowlist, HTTPS/local opt-in, Agent Card validation option, timeout, payload limits, circuit breaker, and normalized results. |
| `src/multimodal_agent/services/agent_communication.py` | implemented | Service boundary and local multi-runtime factory for routing an `AgentTask` through an enabled transport. |
| `src/multimodal_agent/services/agent_pilot_readiness.py` | implemented | Pilot readiness checks, redacted delegated-run metrics summaries, and preview-only failure replay payloads. |
| `src/multimodal_agent/services/agent_gateway.py` | implemented | Local `AgentGateway` that manages `agent.default` plus `agent.worker`, supports `single` and `controller_delegate`, and returns `AgentRunResponse`. |
| `src/multimodal_agent/services/a2a_adapter.py` | implemented | Inbound A2A adapter that maps public agent card and JSON-RPC `SendMessage` requests to/from `AgentGateway`, with public skill filtering. |
| `src/multimodal_agent/tools/agent_delegation_tool.py` | implemented | Opt-in `delegate_to_agent` tool backed by `AgentCommunicationService`. |
| `src/multimodal_agent/api/routes_agent.py` | implemented | Existing `/agent/run` plus separate `/agents/run` gateway endpoint sharing trial access rules. |
| `src/multimodal_agent/api/routes_a2a.py` | implemented | Inbound A2A-compatible agent card and JSON-RPC endpoint over local gateway, including parse/invalid/method/params/internal error mapping. |
| `tests/test_agent_communication_routing.py` | implemented | Offline tests for default routing, local transport, disabled/unknown agents, depth limits, opt-in registration, and local delegation tool behavior. |
| `tests/test_a2a_json_rpc_transport.py` | implemented | Offline fake-server tests for outbound A2A allowlist, HTTPS/local opt-in, Agent Card validation, timeout, payload limit, protocol errors, and business failure normalization. |
| `tests/test_agent_pilot_readiness.py` | implemented | Offline tests for pilot readiness checks, redacted metrics summaries, and failure replay payload redaction. |
| `tests/test_agent_routing_policy.py` | implemented | Offline tests for deterministic gateway routing policy, routing-table overrides, ambiguous capabilities, and config loading. |
| `tests/test_agent_gateway.py` | implemented | Offline tests for gateway routing, controller delegation registry shape, structured unknown-agent failure, and single-agent compatibility. |
| `tests/test_api_a2a.py` | implemented | Offline API tests for public agent card filtering, JSON-RPC `SendMessage`, parse/invalid/method/params/internal errors, context mapping, artifacts, and failed task mapping. |

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
- Agent Card fetch/validation is a verification step for explicitly configured endpoints only; it must not auto-register or auto-enable a remote agent.

## Routing Rules

- Default single-agent routing is `agent.default` and must remain compatible with current CLI/API/demo/eval behavior.
- `POST /agent/run` remains the default single-agent HTTP endpoint and must not use `AgentGateway`.
- `POST /agents/run` is the explicit local multi-agent HTTP endpoint. It accepts `target_agent_id`, optional `capability`, and `collaboration_mode`.
- `/agents/run` responses embed `data.agent_gateway.route_decision` and `runtime_info.agent_gateway.route_decision` with selected agent, requested target/capability, collaboration mode, deterministic route reason, route status, delegation enablement, and structured route error when applicable.
- Gateway route priority is deterministic: explicit `target_agent_id`, configured capability routing table, unique capability match, `controller_delegate` fallback to `agent.default`, then default `agent.default`.
- `GET /.well-known/agent-card.json` exposes a local A2A agent card. It advertises the local `/a2a/rpc` JSON-RPC endpoint, public supported methods, local/offline no-auth status, and enabled local agent skills with public capability filtering.
- Agent Card output must not expose secrets, raw provider details, internal class names, or local file paths.
- `POST /a2a/rpc` supports inbound A2A JSON-RPC `SendMessage` and the legacy-compatible `message/send` alias. It maps text and local media references into `AgentGatewayRunRequest`, then maps `AgentRunResponse` into an A2A task-like result.
- `/a2a/rpc` returns JSON-RPC parse error `-32700`, invalid request `-32600`, method not found `-32601`, invalid params `-32602`, and internal error `-32603` for protocol/adapter failures.
- Gateway/business failures remain successful JSON-RPC responses with an A2A task result whose `status.state` is `failed`.
- A2A metadata may carry `user_id`, `session_id`, `target_agent_id`, `capability`, and `collaboration_mode`. Missing user/session fields use local defaults and still pass through the same trial-access gate.
- `collaboration_mode="single"` directly runs the resolved target agent. If no target or capability is provided, the target is `agent.default`.
- `collaboration_mode="controller_delegate"` enters the controller path when no explicit target is supplied. The controller runtime uses the `agent.default` identity with `delegate_to_agent` registered. The normal single-mode `agent.default` runtime and worker runtimes do not register that tool by default.
- If `target_agent_id` is supplied, it remains the explicit initial route even when `collaboration_mode` is set.
- `delegate_to_agent` is not registered by `create_default_registry()` by default.
- Register `delegate_to_agent` only by passing `enable_agent_delegation=True` and an `AgentCommunicationService` to `create_default_registry(...)`.
- Use `create_local_agent_communication_service({...})` to build a same-process multi-runtime service for tests or explicit local experiments.
- Local multi-runtime factory marks `agent.default` as a controller that can delegate only to non-default local workers; workers default to `can_delegate=False`.
- The current opt-in is code/registry-level or the explicit `/agents/run` gateway. There is no default runtime environment variable that exposes delegation in normal `/agent/run` API/CLI runs.
- Multi-agent routing must be explicit by `agent_id`, capability match, or a configured routing table. Avoid hidden LLM-only target selection until deterministic policy and tests exist.
- A target agent must be enabled in `AgentDirectory`; unknown or disabled agents return structured errors.
- Outbound delegation must use a `delegate_to_agent` style tool through `ActionValidator` and `ToolExecutor`.
- Cross-agent calls must carry `user_id`, `session_id`, parent `run_id`, trace/correlation ID, timeout, and loop/depth metadata.
- AgentCommunicationService applies `AgentDelegationPolicy` before transport dispatch. It blocks self-delegation, sources that cannot delegate, disallowed targets, depth violations, oversized timeout requests, repeated delegation pairs, and ping-pong target pairs.
- Delegated task results include redacted `delegation_audit`, `delegation_pairs`, and optional child budget metadata.
- User/session isolation applies across agent boundaries. One agent must not read another user's memory or session state through delegation.
- Loop control is mandatory. Track delegation depth, repeated target pairs, and ping-pong limits.
- Tool/provider budgets must include delegated work or carry a separate child budget linked to the parent run.
- Remote agents are external capability surfaces. Do not enable them because a URL, API key, or agent card exists.
- Outbound `A2AJsonRpcTransport` requires explicit `AgentInstance(endpoint_url=..., transports=["a2a_json_rpc"])` plus a `RemoteAgentAllowlist`. HTTP is blocked except localhost when explicitly enabled for tests/pilot.
- Outbound A2A supports optional `require_agent_card=True`; the fetched card is bounded and validated against the configured endpoint host, but does not change routing.
- Outbound A2A normalizes missing allowlist, host mismatch, HTTPS policy failure, timeout, HTTP/network failure, payload limit, JSON-RPC protocol error, and remote business failure into `AgentTaskResult.status="failed"`.
- Trace and API output must redact secrets, raw provider responses, inline media/base64 payloads, and remote raw responses.

## Ownership

Module ownership:

| module | responsibility |
| --- | --- |
| `src/multimodal_agent/schemas/agent_communication.py` | Internal message, task, artifact, session ref, route request/result contracts. |
| `src/multimodal_agent/schemas/a2a.py` | Inbound A2A JSON-RPC protocol constants and public request/response wrapper schemas. |
| `src/multimodal_agent/schemas/agent_gateway.py` | External gateway request and metadata contracts for `/agents/run`, including route decision and delegated child-task summaries. |
| `src/multimodal_agent/services/agent_directory.py` | Agent registry, capability metadata, enablement, and directory config loading. |
| `src/multimodal_agent/services/agent_routing_policy.py` | Deterministic route policy, capability match policy, routing-table policy, and gateway route decision assembly. |
| `src/multimodal_agent/services/agent_delegation_policy.py` | Service-layer delegation policy, loop control, allowed-target checks, timeout validation, budget metadata, redaction, and audit event generation. |
| `src/multimodal_agent/services/agent_gateway.py` | Gateway service that selects the initial local runtime and augments `AgentRunResponse` with gateway metadata. |
| `src/multimodal_agent/services/a2a_adapter.py` | Protocol adapter between inbound A2A agent card/JSON-RPC payloads and internal gateway requests/responses. |
| `src/multimodal_agent/services/agent_communication.py` | Service boundary for sending messages/tasks through transports. |
| `src/multimodal_agent/services/agent_transports.py` | `LocalAgentTransport`, `A2AJsonRpcTransport`, outbound allowlist/card/timeout/payload/circuit-breaker controls, and transport result normalization. |
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

Route decisions are reported with these deterministic reasons:

- `explicit_target_agent_id`
- `capability_match`
- `routing_table`
- `controller_delegate_default`
- `default_agent`

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

The adapter extracts text from A2A message parts, maps image/video/audio file references when MIME types are available, reads local routing fields from metadata, maps `contextId` to local `session_id`, and runs the request through `AgentGateway`. A failed agent run is returned as an A2A task with `status.state="failed"`; malformed JSON-RPC or unsupported methods return JSON-RPC error objects. Successful and failed task outputs return final text through A2A artifacts rather than only status messages.

## A2A-Compatible MVP

Implementation order:

1. Add internal schemas and `AgentDirectory`. Done.
2. Add deterministic `AgentRoutingPolicy`, directory config, and routing-table tests. Done.
3. Add `LocalAgentTransport` and same-process multi-runtime tests. Done.
4. Add `AgentCommunicationService`. Done.
5. Add an opt-in `delegate_to_agent` tool. Done for local transport.
6. Add service-layer `AgentDelegationPolicy` for allowed targets, depth, timeout, loop control, budget metadata, redaction, and audit events. Done.
7. Add a local multi-runtime factory for `agent.default -> agent.worker` style tests. Done.
8. Add local `AgentGateway` and separate `/agents/run` API. Done.
9. Add inbound A2A-compatible agent card and JSON-RPC `SendMessage` route. Done.
10. Harden inbound A2A card filtering, public method/auth metadata, context mapping, artifact mapping, and JSON-RPC error taxonomy. Done.
11. Add local delegation context filtering, memory-scope metadata, tool-result pruning, child budget metadata, and artifact summaries. Done.
12. Add outbound `A2AJsonRpcTransport` with allowlist, timeouts, Agent Card validation option, payload limits, circuit breaker, and structured errors. Done.
13. Add pilot readiness checks, redacted cost/latency/artifact/error summaries, and preview-only failure replay payloads. Done.

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
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_routing_policy.py tests/test_agent_gateway.py tests/test_api_agent_graph_runtime.py tests/test_api_a2a.py
```

For broader behavior changes, run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_*.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
