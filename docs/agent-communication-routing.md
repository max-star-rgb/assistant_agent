# Agent Communication Routing

Last updated: 2026-07-21

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Multi-agent instance routing、delegation 与 A2A adapter 的当前权威 |
| Owns | AgentDirectory、routing/delegation policy、transport、control plane、A2A JSON-RPC 与隔离边界 |
| Does not own | 默认单 Agent loop、Gateway frame、Tool 执行、Memory/context 内部策略 |
| 源码与 schema 入口 | `src/assistant_agent/multi_agent/`、`src/assistant_agent/api/routes_a2a.py` |
| 验证入口 | `docs/authority.toml` 中 `agent-communication.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Gateway 见 [`gateway-architecture.md`](gateway-architecture.md) |

This document is the current canonical entry for multi-agent instance routing, agent-to-agent communication, and A2A-style protocol adapter boundaries. Update it whenever agent directory/router behavior, agent communication services, cross-instance sessions, A2A routes, JSON-RPC transport, or related safety policy changes.

Current status: opt-in local AgentRouter boundary, inbound A2A JSON-RPC adapter, default-disabled outbound A2A JSON-RPC pilot transport, read-only control-plane observability, explicit local JSONL durable delegation trace for readiness/pilot use, first-pass pilot operator workflow, strict local pilot evidence package, and breaking cleanup of old router/runtime compatibility imports implemented. The repository still keeps the existing `/agent/run`, CLI, eval, and realtime Gateway paths on one default `AgentGraphRuntime`. It now has a public `assistant_agent.multi_agent` aggregate entrypoint, protocol-neutral schemas, an `AgentDirectory`, deterministic `AgentRoutingPolicy`, `AgentDelegationPolicy`, `LocalAgentTransport`, `A2AJsonRpcTransport`, `AgentCommunicationService`, a local multi-runtime factory, and an `AgentRouter` service with a default process-local redacted control-plane store plus opt-in `JsonlAgentControlPlaneStore`. The former `delegate_to_agent` Tool is temporarily removed, so no Registry exposes model-driven delegation. Explicit `/agents/run` routing, read-only control-plane APIs, pilot scripts, inbound A2A routes, and outbound transport controls remain available.

Current stage boundary:

```text
Current stage implements a local same-process AgentRouter, not a full OpenClaw chat/network fabric.
Default product entrypoints still call agent.default through existing `/agent/run`, CLI, eval, and realtime Gateway paths.
`/agents/run` is the explicit multi-agent router/debug entrypoint.
`/.well-known/agent-card.json` and `/a2a/rpc` expose an inbound A2A-compatible JSON-RPC adapter over the local AgentRouter.
delegate_to_agent is temporarily unavailable; explicit target/capability routing remains available through AgentRouter.
outbound A2A exists only as an explicitly configured transport on an enabled AgentDirectory entry.
durable delegation trace exists only when code explicitly provides JsonlAgentControlPlaneStore.
Any implementation must not change default single-agent behavior.
```

## Scope

Agent communication covers optional collaboration among multiple agent runtime instances. It is separate from:

- Graph Memory nodes: long-term user/project memory, owned by the active `MemoryNodeBundle`.
- Context engineering: prompt/context pack construction, conversation history, observation compaction, and budget reporting.
- Provider adapters: real or mock LLM/image/video/product/provider integrations.
- MCP tools: tool exposure boundary. MCP is not the agent-to-agent communication model.

The intended design is OpenClaw-like at the runtime level: gateway, directory, session/task routing, allowlist, loop limits, and isolated agent instances. A2A JSON-RPC can be one transport adapter for interoperability, not the core internal model.

## Default Baseline

The default path remains single-instance and unchanged:

```text
User / CLI / API / realtime entry
  -> FastAPI routes or local runner
  -> AgentGraphRuntime / assistant loop
  -> AssistantTextOutput | AssistantToolCall
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> Tool / Adapter / memory_recall + memory_commit nodes
```

When only `agent.default` exists, routing should behave like the current implementation. Do not introduce router behavior that changes default mock/local/offline runs.

## Target Multi-Agent Shape

Gateway is the outer business entrypoint for external connections, call lifecycle, session/run/cancel/interrupt, user-level session reuse, and initial agent selection. AgentRouter is the internal multi-agent routing component; it handles deterministic agent selection, capability routing, controller/worker routing, and control-plane records. The compromise layout is `assistant_agent.multi_agent` as the public aggregate import surface while schemas, services, tools, and API routes remain in their existing ownership layers. Do not reintroduce the legacy `runTime` OpenClaw/Anthropic agent loop into this project.

Optional multi-instance routing should be layered as:

```text
User / API / Web UI
  -> Gateway when call/session lifecycle is involved, otherwise existing API route
  -> GatewayRuntimeAdapter / GatewayRuntimeAdapter when a realtime Gateway turn enters assistant execution
  -> AgentGraphRuntime / assistant loop
  -> no model-callable delegation tool (temporarily removed)
  -> AgentCommunicationService
  -> AgentTransport
  -> worker AgentGraphRuntime
  -> existing assistant loop and tool boundary
```

For realtime traffic, `call.incoming`, `call.hangup`, WebSocket frames, media-entry events, and cancel/interrupt lifecycle semantics stop at Gateway and the realtime adapter. Worker agents receive protocol-neutral `AgentTask` / `UserRequest` payloads through `AgentTransport`; they must not depend on Gateway frame names or telephony/WebSocket event shapes.

`POST /agents/run` remains the explicit multi-agent router/debug/service entrypoint:

```text
POST /agents/run
  -> AgentRouter
  -> AgentDirectory
  -> target AgentGraphRuntime
```

Do not route realtime Gateway turns directly from `GatewayRuntimeAdapter` into `AgentRouter`. Realtime multi-agent behavior should pass through the main runtime first, then delegate through the tool-governed boundary.

Agent-to-agent delegation should remain a tool-governed action:

```text
Agent A assistant decision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> no model-callable delegation tool (temporarily removed)
  -> AgentCommunicationService
  -> AgentTransport
  -> Agent B
```

This preserves the existing safety boundary. Assistant nodes must not directly call another runtime, HTTP endpoint, A2A client, queue, or remote SDK.

## Core Concepts

Planned internal contracts:

- `AgentInstance`: one configured runtime identity such as `agent.default`, `agent.vision`, or `agent.product`.
- `AgentDirectory`: registry of agent IDs, capabilities, endpoint metadata, enabled transports, and allowlist policy.
- `AgentRoutingPolicy`: deterministic router policy for explicit targets, routing tables, capability matches, controller fallback, and default fallback.
- `AgentRouter`: internal component that selects the initial target agent for explicit multi-agent requests.
- `AgentDelegationPolicy`: service-layer policy for source permission, allowed targets, depth, timeout, loop detection, budget metadata, and redacted audit events.
- `DelegationContextBuilder`: service-layer context boundary that filters parent history, memory context, raw payloads, and arbitrary metadata before dispatching a child task.
- `AgentCommunicationService`: application service used by tools or API routes to send messages/tasks to another agent.
- `AgentTransport`: transport interface. Implementations may include local in-process calls, A2A JSON-RPC over HTTP, or a future queue.
- `AgentMessage`: internal message envelope independent of any external protocol.
- `AgentTask`: internal task/request envelope with target, session, correlation, timeout, and budget fields.
- `AgentArtifact`: structured output reference or summary returned from a delegated task.
- `AgentSessionRef`: user/session/parent-run/correlation identity for isolation and traceability.

These contracts and implementations are co-located in `multi_agent/`; transport-specific details must not leak into Runtime or Tool implementations.

Implemented files:

| module | status | responsibility |
| --- | --- | --- |
| `src/assistant_agent/multi_agent/__init__.py` | implemented | Lightweight package marker; callers import concrete owned modules instead of triggering an eager aggregate import. |
| `src/assistant_agent/multi_agent/models.py` | implemented | Internal message, task, artifact, session ref, route request/result, and directory config contracts. |
| `src/assistant_agent/multi_agent/a2a_protocol.py` | implemented | Inbound A2A JSON-RPC request/response, task result, artifact, message, and public agent-card schemas. |
| `src/assistant_agent/multi_agent/router_models.py` | implemented | External `/agents/run` request contract, collaboration mode enum, route-decision metadata, and delegated-task summaries. |
| `src/assistant_agent/multi_agent/agent_directory.py` | implemented | Default `agent.default` identity, capability metadata, enablement, and directory config loading. |
| `src/assistant_agent/multi_agent/agent_routing_policy.py` | implemented | Deterministic agent-router policy, capability matching, routing-table overrides, and controller/default fallback selection. |
| `src/assistant_agent/multi_agent/agent_delegation_policy.py` | implemented | Delegation permission, allowed-target, depth, timeout, ping-pong loop, budget metadata, redaction, and audit policy. |
| `src/assistant_agent/multi_agent/agent_delegation_context.py` | implemented | Child-safe delegation context builder, memory scope filter, tool-result reference pruning, child budget metadata, and artifact summaries. |
| `src/assistant_agent/multi_agent/agent_transports.py` | implemented | `LocalAgentTransport` for same-process runtime calls plus default-disabled `A2AJsonRpcTransport` with allowlist, HTTPS/local opt-in, Agent Card validation option, timeout, payload limits, circuit breaker, and normalized results. |
| `src/assistant_agent/multi_agent/agent_communication.py` | implemented | Service boundary and local multi-runtime factory for routing an `AgentTask` through an enabled transport. |
| `src/assistant_agent/multi_agent/agent_pilot_readiness.py` | implemented | Pilot readiness checks, redacted delegated-run metrics summaries, and preview-only failure replay payloads. |
| `src/assistant_agent/multi_agent/agent_router.py` | implemented | Local `AgentRouter` that manages `agent.default` plus `agent.worker`, supports `single` and `controller_delegate`, and returns `AgentRunResponse`. |
| `src/assistant_agent/multi_agent/control_plane_models.py` | implemented | Stable redacted response schemas for read-only control-plane run, route, delegation, budget, audit-event, and replay-preview views. |
| `src/assistant_agent/multi_agent/agent_control_plane.py` | implemented | Process-local control-plane record/audit-event store and query service over router records plus trace summaries. |
| `src/assistant_agent/multi_agent/a2a_adapter.py` | implemented | Inbound A2A adapter that maps public agent card and JSON-RPC `SendMessage` requests to/from `AgentRouter`, with public skill filtering. |
| `src/assistant_agent/api/routes_agent.py` | implemented | Existing `/agent/run` plus separate `/agents/run` router/debug endpoint sharing trial access rules. |
| `src/assistant_agent/api/routes_a2a.py` | implemented | Inbound A2A-compatible agent card and JSON-RPC endpoint over local AgentRouter, including parse/invalid/method/params/internal error mapping. |
| `docs/development/agent-pilot-operator-runbook.md` | implemented | Operator runbook for local/pilot startup, auth/readiness verification, smoke requests, redacted evidence collection, and backout. |

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
- Inbound A2A routes must be thin protocol adapters over `AgentRouter` or `AgentCommunicationService`; they must not directly call a provider, memory store, or another runtime.
- Add `A2AJsonRpcTransport` only behind an `AgentTransport` interface.
- Remote network transport must be explicit opt-in and allowlisted.
- A2A failure must return structured errors; it must not silently fall back to local/mock success.
- Agent Card fetch/validation is a verification step for explicitly configured endpoints only; it must not auto-register or auto-enable a remote agent.

## Routing Rules

- Default single-agent routing is `agent.default` and must remain compatible with current CLI/API/demo/eval behavior.
- `POST /agent/run` remains the default single-agent HTTP endpoint and must not use `AgentRouter`.
- `POST /agents/run` is the explicit local multi-agent HTTP endpoint. It accepts `target_agent_id`, optional `capability`, and `collaboration_mode`.
- `/agents/run` responses embed `data.agent_router.route_decision` and `runtime_info.agent_router.route_decision` with selected agent, requested target/capability, collaboration mode, deterministic route reason, route status, delegation enablement, and structured route error when applicable.
- AgentRouter runs are recorded in a process-local redacted control-plane store by default. Read-only `/control-plane/runs/{run_id}`, `/control-plane/traces/{trace_id}`, `/control-plane/runs/{run_id}/route`, `/control-plane/runs/{run_id}/delegation-tree`, `/control-plane/runs/{run_id}/budget`, `/control-plane/runs/{run_id}/audit`, `/control-plane/audit/events`, `/control-plane/runs/{run_id}/replay-preview`, and `/control-plane/readiness` expose route, delegation, identity provenance, budget/latency, audit decisions, readiness, and replay-preview summaries without raw provider payloads, auth tokens, inline media bodies, hidden reasoning, raw memory content, or parent conversation history.
- `JsonlAgentControlPlaneStore` is an explicit local durable store for readiness and pilot evidence. It persists redacted route records and audit events to a caller-provided JSONL file, keeps latest record by run/trace on read, and does not enable default multi-agent routing, remote discovery, remote A2A, or user-facing control actions.
- Current audit event types include `auth_decision`, `route_decision`, `delegation_decision`, `remote_a2a_decision`, `provider_opt_in_decision`, `memory_access`, `memory_export`, and `memory_delete`. Default audit retention is process-local and replay-safe only; JSONL retention is durable only for explicit local files until deleted by the operator.
- AgentRouter route priority is deterministic: explicit `target_agent_id`, configured capability routing table, unique capability match, `controller_delegate` fallback to `agent.default`, then default `agent.default`.
- `GET /.well-known/agent-card.json` exposes a local A2A agent card. It advertises the local `/a2a/rpc` JSON-RPC endpoint, public supported methods, local/offline no-auth status, and enabled local agent skills with public capability filtering.
- Agent Card output must not expose secrets, raw provider details, internal class names, or local file paths.
- `POST /a2a/rpc` supports inbound A2A JSON-RPC `SendMessage` and the legacy-compatible `message/send` alias. It maps text and local media references into `AgentRouteRequest`, then maps `AgentRunResponse` into an A2A task-like result.
- `/a2a/rpc` returns JSON-RPC parse error `-32700`, invalid request `-32600`, method not found `-32601`, invalid params `-32602`, and internal error `-32603` for protocol/adapter failures.
- Router/business failures remain successful JSON-RPC responses with an A2A task result whose `status.state` is `failed`.
- `/agent/run`, `/agents/run`, WebSocket, memory APIs, and `/a2a/rpc` all pass through the same `AuthContext` dependency and identity resolver. By default `api/auth.py` returns anonymous context and ignores auth-like headers, so request body/path/query/A2A metadata identity remains local/offline provenance rather than production authentication.
- `api/auth.py` now has an `AuthProvider` boundary. `MULTIMODAL_AGENT_AUTH_MODE=anonymous|header_pilot|trusted_header|jwt|session` selects the provider shape; `jwt` and `session` are deferred stubs that return anonymous context until implemented. The legacy `MULTIMODAL_AGENT_AUTH_HEADER_ENABLED=1` still enables `header_pilot` when `MULTIMODAL_AGENT_AUTH_MODE` is unset.
- The disabled-by-default header-auth modes read only controlled `X-Multimodal-Agent-User-Id`, `X-Multimodal-Agent-Session-Id`, `X-Multimodal-Agent-Tenant-Id`, `X-Multimodal-Agent-Project-Id`, and `X-Multimodal-Agent-Scopes` headers into `AuthContext`. Body/path/query/A2A metadata `user_id` must match the header user or the request is rejected before router dispatch.
- `MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY=true` rejects request-derived identity at the API boundary. HTTP routes return `403` with stable `IDENTITY_NOT_AUTH_BOUND` detail, WebSocket sends `agent_error` and closes with policy violation, and `/a2a/rpc` returns JSON-RPC invalid params `-32602`.
- A2A metadata may carry `user_id`, `session_id`, `target_agent_id`, `capability`, and `collaboration_mode`. Missing user/session fields use local defaults and still pass through the same `AuthContext` dependency, request-identity resolver, and trial-access gate. When header auth is enabled, the resolved auth-bound `user_id`/`session_id` and safe `request_identity` provenance are written back into the internal `AgentRouteRequest` before dispatch.
- `collaboration_mode="single"` directly runs the resolved target agent. If no target or capability is provided, the target is `agent.default`.
- `collaboration_mode="controller_delegate"` remains a compatible controller-route value, but the controller currently has no delegation tool and reports `delegation_enabled=false`.
- If `target_agent_id` is supplied, it remains the explicit initial route even when `collaboration_mode` is set.
- `create_default_registry()` 和显式 multi-agent 入口都不装配 `delegate_to_agent`；该 Tool 已暂时删除。
- Use `create_local_agent_communication_service({...})` to build a same-process multi-runtime service for tests or explicit local experiments.
- Local multi-runtime factory marks `agent.default` as a controller that can delegate only to non-default local workers; workers default to `can_delegate=False`.
- The current opt-in is code/registry-level or the explicit `/agents/run` router. There is no default runtime environment variable that exposes delegation in normal `/agent/run` API/CLI runs.
- Multi-agent routing must be explicit by `agent_id`, capability match, or a configured routing table. Avoid hidden LLM-only target selection until deterministic policy and tests exist.
- A target agent must be enabled in `AgentDirectory`; unknown or disabled agents return structured errors.
- Outbound delegation must use a `delegate_to_agent` style tool through `ActionValidator` and `ToolExecutor`.
- Cross-agent calls must carry `user_id`, `session_id`, parent `run_id`, trace/correlation ID, timeout, and loop/depth metadata.
- AgentCommunicationService applies `AgentDelegationPolicy` before transport dispatch. It blocks self-delegation, sources that cannot delegate, disallowed targets, depth violations, oversized timeout requests, repeated delegation pairs, and ping-pong target pairs.
- Delegated task results include redacted `delegation_audit`, `delegation_pairs`, and optional child budget metadata.
- User/session isolation applies across agent boundaries. One agent must not read another user's memory or session state through delegation.
- Loop control is mandatory. Track delegation depth, repeated target pairs, and ping-pong limits.
- Delegated work that carries an explicit child context or tool budget must keep it linked to the parent run.
- Remote agents are external capability surfaces. Do not enable them because a URL, API key, or agent card exists.
- Outbound `A2AJsonRpcTransport` requires explicit `AgentInstance(endpoint_url=..., transports=["a2a_json_rpc"])` plus a `RemoteAgentAllowlist`. HTTP is blocked except localhost when explicitly enabled for tests/pilot.
- Outbound A2A supports optional `require_agent_card=True`; the fetched card is bounded and validated against the configured endpoint host, but does not change routing.
- Outbound A2A normalizes missing allowlist, host mismatch, HTTPS policy failure, timeout, HTTP/network failure, payload limit, JSON-RPC protocol error, and remote business failure into `AgentTaskResult.status="failed"`.
- Trace and API output must redact secrets, raw provider responses, inline media/base64 payloads, and remote raw responses.

## Ownership

Module ownership:

| module | responsibility |
| --- | --- |
| `src/assistant_agent/multi_agent/__init__.py` | Lightweight package marker; public objects remain owned by concrete modules. |
| `src/assistant_agent/multi_agent/models.py` | Internal message, task, artifact, session ref, route request/result contracts. |
| `src/assistant_agent/multi_agent/a2a_protocol.py` | Inbound A2A JSON-RPC protocol constants and public request/response wrapper schemas. |
| `src/assistant_agent/multi_agent/router_models.py` | External router request and metadata contracts for `/agents/run`, including route decision and delegated child-task summaries. |
| `src/assistant_agent/multi_agent/agent_directory.py` | Agent registry, capability metadata, enablement, and directory config loading. |
| `src/assistant_agent/multi_agent/agent_routing_policy.py` | Deterministic route policy, capability match policy, routing-table policy, and router route decision assembly. |
| `src/assistant_agent/multi_agent/agent_delegation_policy.py` | Service-layer delegation policy, loop control, allowed-target checks, timeout validation, budget metadata, redaction, and audit event generation. |
| `src/assistant_agent/multi_agent/agent_router.py` | Router service that selects the initial local runtime and augments `AgentRunResponse` with router metadata. |
| `src/assistant_agent/multi_agent/agent_control_plane.py` | Process-local redacted control-plane run/audit store and query service. |
| `src/assistant_agent/multi_agent/control_plane_models.py` | Control-plane response schemas for run, route, delegation, budget, audit, and replay-preview views. |
| `src/assistant_agent/multi_agent/a2a_adapter.py` | Protocol adapter between inbound A2A agent card/JSON-RPC payloads and internal router requests/responses. |
| `src/assistant_agent/multi_agent/agent_communication.py` | Service boundary for sending messages/tasks through transports. |
| `src/assistant_agent/multi_agent/agent_transports.py` | `LocalAgentTransport`, `A2AJsonRpcTransport`, outbound allowlist/card/timeout/payload/circuit-breaker controls, and transport result normalization. |
| `src/assistant_agent/api/routes_agent.py` | HTTP interface for `/agent/run` and the separate `/agents/run` router route. |
| `src/assistant_agent/api/routes_a2a.py` | Inbound A2A-compatible agent card and local JSON-RPC endpoint. |

If file names change during implementation, keep the same ownership boundaries and update this table.

## Local Multi-Instance Example

The local router and `AgentCommunicationService` remain explicit, same-process boundaries. They are not exposed as a model-callable Tool while `delegate_to_agent` is removed.

## Local AgentRouter API

The explicit HTTP router/debug endpoint is:

```text
POST /agents/run
```

It reuses `AgentRunResponse` and adds `agent_router` metadata under `data` and `runtime_info`. It supports:

- `collaboration_mode="single"`: route directly to `target_agent_id`, capability match, or `agent.default`.
- `collaboration_mode="controller_delegate"`: retained for compatibility and routes to `agent.default` when no explicit target is supplied, but delegation is currently disabled.
- `target_agent_id="agent.worker"`: explicit direct route to the local worker runtime.

Route decisions are reported with these deterministic reasons:

- `explicit_target_agent_id`
- `capability_match`
- `routing_table`
- `controller_delegate_default`
- `default_agent`

The existing `POST /agent/run` endpoint does not use AgentRouter.

Header-auth pilot example:

```bash
MULTIMODAL_AGENT_AUTH_MODE=header_pilot \
MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY=true \
curl -s http://127.0.0.1:8000/agents/run \
  -H 'content-type: application/json' \
  -H 'X-Multimodal-Agent-User-Id: demo_user' \
  -H 'X-Multimodal-Agent-Session-Id: auth_session' \
  -d '{"user_id":"demo_user","session_id":"body_session","text":"你好","target_agent_id":"agent.worker","collaboration_mode":"single"}'
```

If `user_id` is different from `X-Multimodal-Agent-User-Id`, the route returns `403` and does not dispatch to the router.

## Inbound A2A API

The local A2A-compatible discovery and JSON-RPC endpoints are:

```text
GET  /.well-known/agent-card.json
POST /a2a/rpc
```

Supported JSON-RPC methods:

- `SendMessage`: current A2A JSON-RPC message send method.
- `message/send`: compatibility alias routed to the same adapter.

The adapter extracts text from A2A message parts, maps image/video/audio file references when MIME types are available, reads local routing fields from metadata, maps `contextId` to local `session_id`, and runs the request through `AgentRouter`. A failed agent run is returned as an A2A task with `status.state="failed"`; malformed JSON-RPC or unsupported methods return JSON-RPC error objects. Successful and failed task outputs return final text through A2A artifacts rather than only status messages.

When the header-auth pilot is enabled, `/a2a/rpc` applies the same identity binding before `AgentRouter` dispatch. A mismatch between A2A metadata `user_id` and `X-Multimodal-Agent-User-Id` returns JSON-RPC invalid params `-32602`; this is a protocol/adapter rejection, not a failed agent task.

## A2A-Compatible MVP

Implementation order:

1. Add internal schemas and `AgentDirectory`. Done.
2. Add deterministic `AgentRoutingPolicy`, directory config, and routing-table tests. Done.
3. Add `LocalAgentTransport` and same-process multi-runtime tests. Done.
4. Add `AgentCommunicationService`. Done.
5. Add an opt-in `delegate_to_agent` tool. Previously implemented, now temporarily removed.
6. Add service-layer `AgentDelegationPolicy` for allowed targets, depth, timeout, loop control, budget metadata, redaction, and audit events. Done.
7. Add a local multi-runtime factory for `agent.default -> agent.worker` style tests. Done.
8. Add local `AgentRouter` and separate `/agents/run` API. Done.
9. Add inbound A2A-compatible agent card and JSON-RPC `SendMessage` route. Done.
10. Harden inbound A2A card filtering, public method/auth metadata, context mapping, artifact mapping, and JSON-RPC error taxonomy. Done.
11. Add local delegation context filtering, memory-scope metadata, tool-result pruning, child budget metadata, and artifact summaries. Done.
12. Add outbound `A2AJsonRpcTransport` with allowlist, timeouts, Agent Card validation option, payload limits, circuit breaker, and structured errors. Done.
13. Add pilot readiness checks, redacted cost/latency/artifact/error summaries, and preview-only failure replay payloads. Done.

The inbound MVP exposes this repository as an agent without changing default `/agent/run` behavior. The outbound MVP should only call allowlisted local or explicitly configured remote agents.

## Update Rules

- Read this document before designing or changing agent instance routing, agent-to-agent communication, or A2A adapters.
- Keep the `agent-communication` entry in `docs/authority.toml` synchronized with current source ownership and verification.
- Update `README.md` only when the human-facing navigation entry changes; do not copy routing details into `AGENTS.md`.
- If implementation affects architecture layering, update the boundary rules in `AGENTS.md`.
- If delegation affects prompt/context content, also read and update `docs/context_engineering_status.md`.
- If delegation reads or writes memory, also read and update `docs/memory-service-architecture.md`.
- Follow `tests/README.md`; real remote-agent or network smoke checks must be explicit opt-in.

## Validation

Default stable core pytest safety net (bare pytest collects only `tests/core`):

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

For broader behavior checks, run the deterministic eval/demo tools. Pilot-readiness logic currently has no standalone
operator script; import/compile validation is recorded in `docs/authority.toml` until a stable entrypoint exists:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
