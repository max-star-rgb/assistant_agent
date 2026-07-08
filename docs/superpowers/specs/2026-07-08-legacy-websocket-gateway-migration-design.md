# Legacy WebSocket Gateway Migration Design

Date: 2026-07-08

## Goal

Move legacy `/ws/agent/{session_id}` behind Gateway while preserving the
existing WebSocket JSON event contract.

Target internal path:

```text
/ws/agent/{session_id}
    -> legacy WebSocket entry adapter
    -> GatewayTurnFacade
    -> GatewaySessionManager / GatewaySessionService
    -> GatewayAgentAdapter
    -> AssistantRuntimeApp
    -> run_assistant_request
    -> AgentGraphRuntime
```

External clients still receive legacy `AgentEvent` JSON messages such as
`task_started`, `tool_started`, `response_delta`, `final_response`,
`task_failed`, `agent_error`, and final `agent_response`.

## Problem

Gateway emits normalized frames (`run.started`, `event.progress`,
`event.tool`, `stream.chunk`, `run.end`). Existing Web Console, remote CLI, and
tests consume the older `AgentEvent` event stream. Replacing the wire protocol
in this phase would break clients and make Gateway migration too broad.

## Approach

Use Gateway internally and mirror raw runtime events externally.

For each legacy WebSocket connection, the route creates a local
`GatewaySessionManager(start_reaper=False)` and `GatewayTurnFacade`. The
Gateway backend is `GatewayAgentAdapter(run_request=...)`. Inside the backend
callback, the route wraps the Gateway-provided runtime event sink with a mirror
sink:

- one side forwards events to Gateway's realtime forwarder, preserving Gateway
  frames and lifecycle;
- the other side sends the original `AgentEvent` objects to the legacy
  WebSocket queue.

After `GatewayTurnFacade.run_turn()` reaches `run.end`, the route sends the
same final legacy `agent_response` event it sends today, containing the full
`AgentRunResponse`.

## Scope

In scope:

- `/ws/agent/{session_id}` one-turn legacy WebSocket route.
- Existing query and initial JSON request payload parsing.
- Existing identity, trial access, and local CLI bypass behavior.
- Existing WebSocket event names and final `agent_response` payload.

Out of scope:

- `/ws/gateway` normalized Gateway frame route; it already exists.
- `/ws/realtime/media`; it already enters Gateway.
- `/agent-service/v1` vendor compatibility route.
- Remote CLI client protocol changes.
- Observer wiring.

## Error Handling

- Identity and trial-access failures remain unchanged and still send
  `agent_error`.
- Runtime exceptions inside the Gateway turn are converted to legacy
  `agent_error`.
- If Gateway completes without captured assistant artifacts, send legacy
  `agent_error`.
- The local Gateway manager is closed after each legacy WebSocket turn.

## Testing

- Add a WebSocket test that uses a recording runtime and asserts the final
  runtime `UserRequest` includes `metadata["runtime"]["history"]`, proving the
  request passed through `GatewaySessionService`.
- Keep existing WebSocket event stream tests green, including raw event order,
  response deltas, final payload, auth behavior, and structured error events.

## Stop Point

Stop this phase after legacy `/ws/agent/{session_id}` is internally
Gateway-first and its public wire contract remains stable. The next phase
should migrate demo/eval/scenario paths or review whether all product-critical
entries have converged enough to start observer integration.
