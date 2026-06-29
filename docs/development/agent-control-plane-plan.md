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

- `docs/CODEX_PROJECT_GUIDE.md`
- `docs/agent-communication-routing.md`
- `docs/architecture-layers.md`
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
- Default runtime profile remains mock/local/offline.

Unsupported baseline:

- Outbound A2A remote calls.
- Remote agent network fabric.
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

Status: not started.

Goal:

- Make `delegate_to_agent` a high-spec safety tool.

Planned controls:

- Delegation depth policy.
- Ping-pong loop detector.
- Parent/child run budget metadata.
- Timeout policy.
- Delegation audit event.
- Delegation input redaction.

Acceptance checks:

- `default -> worker` can run when allowed.
- `worker -> default` is blocked by default.
- Repeated target-pair loops are blocked or bounded.
- Timeout and budget failures return structured errors.
- Parent/child trace tree can be reconstructed.

## Phase D: Inbound A2A Conformance

Status: not started.

Goal:

- Make the local A2A server compatible enough for external callers without expanding permissions.

Planned controls:

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

## Phase E: Context, Memory, And Budget Across Agents

Status: not started.

Goal:

- Prevent cross-agent context leakage, memory scope bypass, and budget blowups.

Planned modules:

- `DelegationContextBuilder`
- `ChildContextBudget`
- `MemoryScopeFilter`
- `ToolResultPruner`
- `ArtifactSummaryBuilder`
- Parent/child budget report

Acceptance checks:

- Child runs do not receive full parent history.
- Child runs cannot read another user's memory.
- Large tool outputs are summarized or passed by reference.
- Parent receives child artifact/ref/summary instead of raw child context.

## Phase F: Outbound A2A Pilot

Status: not started.

Goal:

- Add outbound `A2AJsonRpcTransport` only as explicit pilot capability.

Required controls:

- Remote agent allowlist.
- Agent Card fetcher and validator.
- Auth header provider.
- Timeout/retry/circuit breaker.
- Max payload size.
- No silent local fallback.

Acceptance checks:

- No allowlist means outbound is forbidden.
- Host mismatch is forbidden.
- Remote timeout returns structured error.
- Remote protocol errors are normalized.
- Fake A2A server tests run offline.

## Phase G: Pilot Readiness

Status: not started.

Goal:

- Make the gateway safe for a limited pilot, not public production.

Required controls:

- Explicit `provider_smoke` / `pilot` real-provider profiles.
- Auth-bound user identity.
- Trace and metrics view.
- Memory isolation tests.
- Cost/latency reports.
- Redaction tests.
- Failure replay notes.

Acceptance checks:

- Default profile still mock/local/offline.
- Remote calls remain opt-in.
- User identity comes from trusted auth context where available.
- Traces are redacted by default.
