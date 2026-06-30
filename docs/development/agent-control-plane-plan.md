# Agent Control Plane Development Plan

Last updated: 2026-06-29

This is the phased development plan for turning the local multi-agent gateway into a pilot-ready control plane:

```text
Default /agent/run stays single-agent and stable.
/agents/run is the explicit local multi-agent gateway.
delegate_to_agent remains a governed tool boundary.
Inbound A2A remains a protocol adapter over the local gateway.
Outbound A2A is opt-in pilot work only.
LLM target selection is suggestion-only until deterministic policy exists.
```

This document is a development plan, not the canonical architecture source. Before changing code, read:

- `AGENTS.md`
- `docs/agent-communication-routing.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md` when context, history, tool observation compaction, or budget behavior changes.
- `docs/memory-service-architecture.md` when memory scope or memory tools change.

## External Guidance

The next stage follows these engineering principles:

- Keep one authoritative run path and preserve request/session ordering.
- Expose agent handoff/delegation as a tool-like governed action, not a direct runtime call from the assistant loop.
- Treat A2A as an interoperability protocol adapter, not the internal model.
- Treat memory as context, not as enforcement policy; enforcement stays in validators, routing policy, and gateway controls.

## Target

The target is:

```text
Pilot-ready Local Multi-Agent Gateway
  + A2A-Compatible Inbound
  + Safe Delegation Control Plane
```

Not in scope for this plan:

- Full OpenClaw clone.
- Public remote agent network fabric.
- Remote agent marketplace.
- Automatic remote Agent Card discovery and enablement.
- LLM-only target-agent selection.

## Current Baseline

Implemented baseline:

- `POST /agent/run`: default single-agent path.
- `POST /agents/run`: explicit local multi-agent gateway.
- Local agents: `agent.default`, `agent.worker`.
- `collaboration_mode="single"`.
- `collaboration_mode="controller_delegate"`.
- `delegate_to_agent`: opt-in tool registered only for the gateway controller runtime.
- Worker runtime does not register `delegate_to_agent`.
- `GET /.well-known/agent-card.json`: inbound A2A-compatible discovery.
- `POST /a2a/rpc`: inbound A2A JSON-RPC `SendMessage` plus `message/send` compatibility alias.
- Default-disabled outbound `A2AJsonRpcTransport` pilot with explicit endpoint and allowlist.
- Pilot readiness summary and preview-only failure replay helpers.
- Default runtime profile remains mock/local/offline.

Unsupported baseline:

- Public remote agent network fabric.
- Automatic remote Agent Card discovery or enablement.
- LLM target-agent selection.
- Automatic real-provider enablement from keys.

## Execution Rules

1. Keep `/agent/run` behavior unchanged unless a task explicitly targets it.
2. Keep default runtime mock/local/offline.
3. Keep internal schemas independent from A2A protocol schemas.
4. Do not let assistant nodes call runtimes, HTTP clients, A2A clients, provider SDKs, or memory stores directly.
5. Route delegation through `AssistantDecision -> ActionValidator -> ToolExecutor -> delegate_to_agent -> AgentCommunicationService -> AgentTransport`.
6. Do not pass raw provider responses, base64/media bodies, secrets, tokens, or full parent context to child agents.
7. Add offline tests for each control-plane behavior before considering pilot/provider smoke.
8. After each phase, update this file with status, changed files, validation commands, and remaining risks.

## Phase A: Status Alignment And Contract Hardening

Status: done.

Goal:

- Make current capabilities official contracts.
- Add stable route-decision metadata for gateway runs.
- Add typed A2A task-like result schemas for inbound adapter output.
- Add OpenAPI examples for `/agents/run` and `/a2a/rpc`.
- Keep `/agent/run` unchanged.

Implementation steps:

1. Add `AgentGatewayRouteDecision` / gateway metadata schema.
2. Include deterministic route reason in `/agents/run` responses:
   - `explicit_target_agent_id`
   - `capability_match`
   - `controller_delegate_default`
   - `default_agent`
3. Keep backward-compatible `data.agent_gateway.agent_id`, `collaboration_mode`, `delegated_tasks`, and `route`.
4. Add typed A2A task/message/artifact result schemas used by `task_from_gateway_response`.
5. Add request examples for `AgentGatewayRunRequest` and `A2AJsonRpcRequest`.
6. Extend tests for route-decision metadata and A2A result schema validation.

Acceptance checks:

- `/agent/run` still uses the normal single-agent runtime.
- `/agents/run` default route reports `default_agent`.
- `/agents/run` explicit worker route reports `explicit_target_agent_id`.
- `/agents/run` capability route reports `capability_match`.
- `/agents/run controller_delegate` reports `controller_delegate_default`.
- Unknown agent returns structured failure with route decision metadata.
- Inbound A2A `SendMessage` validates into typed task result.

Implemented files:

- `src/multimodal_agent/schemas/agent_gateway.py`
- `src/multimodal_agent/services/agent_gateway.py`
- `src/multimodal_agent/schemas/a2a.py`
- `src/multimodal_agent/services/a2a_adapter.py`
- `tests/test_agent_gateway.py`
- `tests/test_api_a2a.py`

Validation run on 2026-06-29:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_gateway.py \
  tests/test_api_a2a.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_agent_communication_routing.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
```

Result:

```text
32 passed
check_env ok=true
```

Remaining risks:

- Route policy is still embedded in `AgentDirectory` and `AgentGateway`; Phase B should extract explicit routing policy/config contracts.
- Delegation safety still relies on current depth/self-delegation checks; Phase C should add allowed-target, repeated-pair, timeout, and budget controls.
- A2A inbound is intentionally minimal; Phase D should harden conformance and public card filtering.

Suggested validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_gateway.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py \
  tests/test_agent_communication_routing.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
```

## Phase B: AgentDirectory And RoutingPolicy Engineering

Status: done.

Goal:

- Make `AgentDirectory` the routing source of truth.
- Move gateway route selection out of scattered conditionals into deterministic policy.

Implemented modules:

- `AgentDirectoryConfig`
- `AgentInstanceConfig`
- `CapabilityMatchPolicy`
- `RoutingTablePolicy`
- `AgentRoutingPolicy`
- `AgentRoutingDecision`

Acceptance checks:

- Explicit `target_agent_id` routes or returns structured unknown/disabled errors.
- Unique capability match routes.
- Multiple capability matches return ambiguous error unless routing table resolves.
- No LLM-only route path exists.

Implemented files:

- `src/multimodal_agent/schemas/agent_communication.py`
- `src/multimodal_agent/schemas/agent_gateway.py`
- `src/multimodal_agent/services/agent_directory.py`
- `src/multimodal_agent/services/agent_routing_policy.py`
- `src/multimodal_agent/services/agent_gateway.py`
- `tests/test_agent_routing_policy.py`
- `tests/test_agent_gateway.py`

Validation run on 2026-06-29:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_routing_policy.py \
  tests/test_agent_gateway.py \
  tests/test_api_a2a.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_agent_communication_routing.py
```

Result:

```text
40 passed
```

Remaining risks:

- Routing policy is deterministic and configurable, but still local-only.
- Routing table entries only select enabled directory agents; they do not yet encode richer allow-target policy or per-agent budgets.
- LLM routing remains intentionally unsupported except as a future suggestion input to policy.

## Phase C: Delegation Safety Hardening

Status: done.

Goal:

- Make `delegate_to_agent` a high-spec safety tool.

Implemented controls:

- Delegation depth policy.
- Ping-pong loop detector.
- Parent/child run budget metadata.
- Timeout policy.
- Delegation audit event.
- Delegation input redaction.
- Source `can_delegate` and `allowed_targets` policy.

Acceptance checks:

- `default -> worker` can run when allowed.
- `worker -> default` is blocked by default.
- Repeated target-pair loops are blocked or bounded.
- Timeout limit failures return structured errors.
- Child token/tool budget metadata is propagated for later accounting.
- Parent/child run, trace, correlation, and audit metadata is carried in task results.

Implemented files:

- `src/multimodal_agent/schemas/agent_communication.py`
- `src/multimodal_agent/services/agent_delegation_policy.py`
- `src/multimodal_agent/services/agent_communication.py`
- `src/multimodal_agent/services/agent_directory.py`
- `src/multimodal_agent/services/agent_gateway.py`
- `src/multimodal_agent/services/agent_transports.py`
- `src/multimodal_agent/tools/agent_delegation_tool.py`
- `tests/test_agent_communication_routing.py`

Validation run on 2026-06-29:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_communication_routing.py \
  tests/test_agent_gateway.py \
  tests/test_agent_routing_policy.py \
  tests/test_api_a2a.py \
  tests/test_api_agent_graph_runtime.py
```

Result:

```text
44 passed
```

Remaining risks:

- Timeout policy validates configured limits before dispatch; local in-process transport still cannot preempt a long-running runtime mid-call.
- Budget fields are propagated as parent/child metadata; actual token/tool accounting remains future budget-integration work.
- Audit metadata is returned with delegated task results; durable trace/replay storage remains a later pilot-readiness concern.

## Phase D: Inbound A2A Conformance

Status: done.

Goal:

- Make the local A2A server compatible enough for external callers without expanding permissions.

Implemented controls:

- Agent Card versioning and public capability filtering.
- Public method list.
- JSON-RPC parse/invalid/method-not-found taxonomy.
- Business failure vs protocol failure separation.
- Artifact output mapping.
- `contextId` / `session_id` mapping rules.

Acceptance checks:

- Agent Card exposes no secrets, provider internals, or local file paths.
- Malformed JSON-RPC returns protocol error.
- Unknown methods return method-not-found.
- Business failures become failed tasks.
- Successful final answer appears as an artifact.

Implemented files:

- `src/multimodal_agent/schemas/a2a.py`
- `src/multimodal_agent/services/a2a_adapter.py`
- `src/multimodal_agent/api/routes_a2a.py`
- `tests/test_api_a2a.py`

Validation run on 2026-06-29:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_api_a2a.py \
  tests/test_agent_gateway.py \
  tests/test_agent_routing_policy.py \
  tests/test_agent_communication_routing.py \
  tests/test_api_agent_graph_runtime.py
```

Result:

```text
50 passed
```

Remaining risks:

- This is inbound compatibility only; outbound A2A transport remains explicitly out of scope until Phase F.
- Agent Card auth is declared as not required for local/offline mode; pilot auth binding remains Phase G work.
- The adapter implements the current local `SendMessage` / `message/send` subset, not the full A2A task lifecycle.

## Phase E: Context, Memory, And Budget Across Agents

Status: done for the local delegation boundary.

Goal:

- Prevent cross-agent context leakage, memory scope bypass, and budget blowups.

Planned modules:

- `DelegationContextBuilder`: implemented in `services/agent_delegation_context.py`.
- `ChildContextBudget`: implemented as child-run budget metadata.
- `MemoryScopeFilter`: implemented to block parent `memory_context_*` forwarding.
- `ToolResultPruner`: implemented to pass tool output references instead of raw parent tool payloads.
- `ArtifactSummaryBuilder`: implemented to attach trace-safe child artifact summaries.
- Parent/child budget report: implemented as `child_context_budget` on child request and task result metadata.

Acceptance checks:

- Child runs do not receive full parent history.
- Child runs cannot read another user's memory.
- Large tool outputs are summarized or passed by reference.
- Parent receives child artifact/ref/summary instead of raw child context.

Implementation notes:

- The context boundary runs inside `AgentCommunicationService` after delegation policy accepts a task and before transport dispatch.
- Child request metadata keeps explicit `context_refs`, `request_origin`, `agent_communication`, `child_context_budget`, and `agent_context`.
- Parent `conversation_history`, `parent_history`, `memory_context_*`, raw provider payloads, base64/media/body fields, secret/token-like fields, and non-allowlisted arbitrary metadata are omitted and recorded in `agent_context.omitted_context`.
- Raw parent `tool_results` are replaced with `tool_result_refs` when an output reference is available.
- Memory retrieval and write policy remain owned by `MemoryManager`; delegation does not move memory logic into tools, transports, or gateway routing.

Validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_routing.py
```

Result:

```text
22 passed
```

## Phase F: Outbound A2A Pilot

Status: done for default-disabled, allowlisted pilot transport.

Goal:

- Add outbound `A2AJsonRpcTransport` only as explicit pilot capability.

Required controls:

- Remote agent allowlist: implemented through `RemoteAgentAllowlist`.
- Agent Card fetcher and validator: implemented as optional `require_agent_card=True` verification for explicitly configured endpoints.
- Auth header provider: implemented through explicit `AuthHeaderProvider`.
- Timeout/retry/circuit breaker: timeout and circuit breaker implemented; retry is intentionally not enabled by default for pilot safety.
- Max payload size: implemented for request, response, and Agent Card payloads.
- No silent local fallback: implemented; all remote failures return structured `AgentTaskResult(status="failed")`.

Acceptance checks:

- No allowlist means outbound is forbidden.
- Host mismatch is forbidden.
- Remote timeout returns structured error.
- Remote protocol errors are normalized.
- Fake A2A server tests run offline.

Implementation notes:

- `A2AJsonRpcTransport` lives behind the `AgentTransport` interface in `services/agent_transports.py`.
- A remote agent must be configured in `AgentDirectory` with `transports=["a2a_json_rpc"]` and an explicit `endpoint_url`.
- The transport requires an explicit host allowlist. HTTPS is required except localhost HTTP when explicitly enabled for tests/pilot.
- Agent Card fetch/validation verifies an already configured endpoint when `require_agent_card=True`; it never auto-registers or auto-enables remote agents.
- JSON-RPC protocol errors and A2A task-level business failures are separated and normalized.
- This phase still does not implement public remote agent fabric, automatic Agent Card discovery, remote marketplace behavior, or LLM target-agent selection.

Validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_a2a_json_rpc_transport.py \
  tests/test_agent_communication_routing.py \
  tests/test_agent_gateway.py \
  tests/test_agent_routing_policy.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py
```

Result:

```text
62 passed
```

## Phase G: Pilot Readiness

Status: minimal readiness/report/replay slice done.

Goal:

- Make the gateway safe for a limited pilot, not public production.

Required controls:

- Explicit `provider_smoke` / `pilot` real-provider profiles: checked by `PilotReadinessChecker`; default `local_demo` remains pass.
- Auth-bound user identity: represented as a readiness check; missing auth-bound identity is a warning for production pilot, not silently accepted as complete. HTTP, WebSocket, and inbound A2A routes now pass through `services/api_identity.py`; `api/auth.py` supplies the default anonymous `AuthContext` dependency and a disabled-by-default header-auth pilot guarded by `MULTIMODAL_AGENT_AUTH_HEADER_ENABLED`. When enabled, only controlled `X-Multimodal-Agent-*` headers become trusted `AuthContext` fields, and request body/path/query identity mismatch is rejected. `IdentityPolicy` turns that provenance into machine-readable `passed` / `warning` / `failed` decisions for pilot readiness.
- Trace and metrics view: implemented for delegated task results through `PilotRunSummary`.
- Memory isolation tests: not expanded in this slice; existing memory isolation work remains under memory hardening.
- Cost/latency reports: implemented as redacted summary fields; `AgentCommunicationService` now attaches `latency_ms` around transport dispatch.
- Redaction tests: implemented for pilot summaries and replay payloads.
- Failure replay notes: implemented through `FailureReplayPayload`.

Acceptance checks:

- Default profile still mock/local/offline.
- Remote calls remain opt-in.
- User identity comes from trusted auth context where available; current local/offline routes record request-derived identity provenance but are not production-auth-bound.
- Traces are redacted by default.

Implementation notes:

- `services/agent_pilot_readiness.py` adds `PilotReadinessChecker`, `PilotRunSummary`, and `FailureReplayPayload`.
- Readiness checks cover default runtime profile, remote A2A explicit opt-in and allowlist presence, `IdentityPolicy` auth-bound identity status, and trace redaction boundary.
- Failure replay payloads include preview-only user text, media counts, budgets, routing metadata, error codes, and sanitized result metadata. They omit raw provider/tool payloads, parent history, secrets, and full message bodies.
- This phase does not enable real providers, remote calls, or automatic remote discovery. It also does not complete production auth; request-derived identity from body/path/query/WebSocket/A2A metadata remains a warning until the `AuthContext` dependency returns an auth-bound principal.

Validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_communication_routing.py \
  tests/test_agent_pilot_readiness.py \
  tests/test_a2a_json_rpc_transport.py \
  tests/test_agent_gateway.py \
  tests/test_agent_routing_policy.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py
```

Result:

```text
67 passed across the current gateway/A2A/pilot readiness regression slice.
```

Known validation limitation:

- No public remote agent fabric, automatic Agent Card discovery, or production auth binding is implemented in this phase.
