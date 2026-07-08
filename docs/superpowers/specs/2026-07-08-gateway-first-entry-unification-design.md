# Gateway-First Entry Unification Design

Date: 2026-07-08

## Goal

Align product entry routing around Gateway as the lifecycle boundary before
adding observer wiring.

The target direction is:

```text
Web / CLI / HTTP / WebSocket / realtime adapters
    -> Gateway ingress adapters
    -> GatewaySessionManager / GatewaySessionService
    -> GatewayAgentAdapter
    -> AssistantRuntimeApp
    -> run_assistant_request
    -> AgentGraphRuntime / assistant loop
```

`AssistantRuntimeApp` remains useful, but it is not the final product entry
boundary. It is the backend-to-runtime boundary behind Gateway.

## Problem

Phase 5 extracted `AssistantRuntimeApp` and moved several callers to it. That
reduced direct `AgentGraphRuntime` construction in product code, but it left the
product direction wrong for the next architecture step: Web, CLI, HTTP, and
WebSocket should converge through Gateway before observer work. If observers are
wired while entries still bypass Gateway, lifecycle, cancellation, stream-frame,
and trace semantics will remain split.

## Phase 6A Scope

Build the smallest reusable sync-turn facade over existing Gateway services.

The facade accepts a normalized user turn, sends a `message.user` frame through
a `GatewaySessionManager` endpoint, collects outbound Gateway frames until
`run.end`, and returns a structured `GatewayTurnResult`.

This phase does not migrate `/agent/run`, `/ws/agent`, or the local CLI yet.
Those migrations need a separate compatibility step because HTTP currently
returns the rich `AgentRunResponse` schema, while the Gateway wire protocol only
emits stream frames plus terminal `run.end` metadata.

## Components

- `assistant_agent.services.gateway_turn_facade`
  - `GatewayTurnRequest`: input contract for a single Gateway-normalized turn.
  - `GatewayTurnResult`: frames, terminal frame, response text assembled from
    `stream.chunk`, status, run id, turn id, trace id, and terminal payload.
  - `GatewayTurnFacade`: owns the sync turn algorithm while depending on an
    injected `GatewaySessionManager`.

- `assistant_agent.api.gateway_runtime`
  - Provides a process-local `GatewayTurnFacade` factory beside the existing
    Gateway session manager and bridge.

## Data Flow

```text
GatewayTurnRequest
    -> GatewayTurnFacade.run_turn()
    -> manager.acquire(user_id, config)
    -> endpoint.send(message.user)
    -> collect run.started / event.progress / stream.chunk / run.end
    -> GatewayTurnResult
```

The facade preserves Gateway-owned session history, run id generation, timeout,
interrupt, cancellation, and backend event mapping because it does not call
`GatewayAgentAdapter` or `AssistantRuntimeApp` directly.

## Error Handling

- Missing `session_id` continues to be a Gateway protocol error.
- A terminal `run.end` with `reason="error"` is returned as a structured result
  instead of being raised.
- Endpoint closure before `run.end` raises `GatewayTurnError`.
- Timeout while waiting for `run.end` raises `GatewayTurnTimeout`.

## Testing

- Unit tests use a fake `RealtimeAgentBackend` behind
  `GatewaySessionManager(start_reaper=False)`.
- Tests assert the facade sends turns through Gateway by checking backend
  `RealtimeAgentRequest` metadata and Gateway history.
- Tests assert `GatewayTurnResult.response_text`, `trace_id`, terminal reason,
  and collected frame types.
- Tests assert backend error frames become a structured result rather than an
  exception.

## Stop Point

Stop Phase 6A after the facade is implemented, documented, and covered by
focused tests. The next phase should migrate HTTP `/agent/run` to consume this
facade while preserving the public `AgentRunResponse` contract or documenting a
new Gateway-specific response schema.
