# Realtime Agent Interrupt Phase 2 Plan

Last updated: 2026-07-02

This document refines Phase 2 of `docs/development/realtime_phone_backend_plan.md`.
The original MVP is already implemented: `AgentGraphRealtimeBackend` accepts a
neutral cancel token, `GatewaySessionService` handles `run.cancel` and
same-session interruption, and the Gateway emits `run.end` with
`reason="cancelled"` when the boundary observes cancellation.

Phase 2 is now implemented for the internal cooperative cancellation path. The
Gateway can still cancel before a run starts or after it returns, and the
assistant runtime now also observes the same run-scoped token inside the graph
and tool execution chain.

## Purpose

Phase 2 should make interruption a first-class internal agent signal.

The target behavior is:

- Gateway receives `run.cancel` or a newer `message.user` for the same session.
- Gateway marks the active run's cancel token.
- `AgentGraphRealtimeBackend` passes that token into the assistant runtime.
- The assistant graph, tool execution path, and selected provider boundaries
  check the token at safe checkpoints.
- When cancellation is observed, the run stops with a structured cancelled
  result instead of continuing to produce stale output.

This does not require true token streaming. Phase 3 streaming improves user
experience, but Phase 2 can be completed while model output is still
non-streaming.

## Current State

Implemented:

- `src/assistant_agent/realtime/types.py` defines `RealtimeCancelToken`.
- `AgentGraphRealtimeBackend.run_turn(...)` checks cancellation before and after
  `run_assistant_request(...)`.
- `GatewaySessionService` creates one `CancelToken` per run and passes it into
  the realtime backend.
- `GatewaySessionService` cancels an active run when it receives `run.cancel`.
- `GatewaySessionService` interrupts an active same-session run before starting
  the next message.
- Gateway/session tests cover explicit cancel, interrupt, history, and
  `expects_reply`.

Implemented in Phase 2:

- `run_assistant_request(...)` accepts an optional `cancel_token` and passes it
  to `AgentGraphRuntime.run_state(...)` when the runtime supports it.
- `AgentGraphRealtimeBackend.run_turn(...)` passes its token into the shared
  assistant run service and maps internal `AgentState.status == "cancelled"` to
  a realtime cancelled result.
- `AgentGraphRuntime` carries the token as per-run state, marks cancelled runs
  with `AgentState.cancel(...)`, records run history/session status as
  `cancelled`, and emits `task_cancelled` instead of `final_response`.
- `GraphRuntimeContext` carries the token and `bind_runtime_node(...)` checks it
  before node execution and after node completion.
- `ToolExecutor` checks cancellation before a tool call, before each retry
  attempt, after tool execution returns, and around retry sleep.
- `ToolContext` exposes `cancel_token` and `is_cancelled()` for tools with
  natural polling points.
- The internal cancellation contract lives in
  `assistant_agent.agent.cancellation` and is independent of Gateway frames and
  runTime/OpenClaw types.
- API error mapping exposes `AGENT_RUN_CANCELLED` with safe cancellation detail
  such as `cancel_phase`, `node_name`, and `tool_name`.

Known limits:

- Current `ChatAdapter.chat(...)` calls are synchronous. Phase 2 checks before
  and after graph nodes, so a blocking provider call still relies on provider
  timeout until it returns.
- Existing tools are not forced to become async. Tools that need faster
  cancellation can poll `context.is_cancelled()` at their own internal
  checkpoints.

## Phase 2 Implementation Notes

1. Add a neutral internal cancellation contract.

   - Implemented as `AgentRunCancelled`, `is_cancelled(...)`, and
     `raise_if_cancelled(...)` in `assistant_agent.agent.cancellation`.
   - The contract accepts any object with `is_cancelled()` and stays independent
     of Gateway/runTime types.

2. Thread cancellation through the assistant run service.

   - `cancel_token` defaults to `None`, so existing callers are unchanged.
   - Runtime/test doubles that do not accept `cancel_token` are still supported.

3. Add graph-level checkpoints.

   - Cancellation is checked before graph execution begins, at node entry, and
     after node completion.
   - Observed cancellation becomes `AgentState.status == "cancelled"`.
   - Normal `/agent/run` behavior is unchanged unless a caller explicitly passes
     a token.

4. Add tool execution checkpoints.

   - Cancellation before a tool call skips execution.
   - Cancellation after a tool returns discards the tool result and stops the
     run as cancelled.
   - `ToolContext.is_cancelled()` allows cooperative early return inside tools.

5. Add provider boundary protection.

   - Current synchronous `ChatAdapter.chat(...)` compatibility is preserved.
   - Provider/model calls are protected by graph node boundary checks and
     configured provider timeouts.
   - Provider-level hard cancel or delta streaming remains Phase 3 work.

6. Preserve Gateway wire behavior.

   - `run.cancel` should still be accepted by run id or session id.
   - Same-session new message should still interrupt the previous run.
   - `run.end` should use `reason="cancelled"` when the internal agent observes
     cancellation.
   - Cancelled runs should not emit final `response.chunk` or `response.final`
     after cancellation is observed.

## Phase 2.5: Deadlines And Timeout Hardening

Phase 2 cancellation can still feel slow when a provider or tool is blocked in a
synchronous call. Add a small follow-up phase before true streaming:

- Add optional run deadline metadata to the realtime request path.
- Convert deadline expiry into the same internal cancellation contract.
- Ensure provider/tool timeout settings are visible in cancellation metadata.
- Add tests for timeout/deadline conversion to cancelled/error outcomes.

This is a pragmatic bridge: it improves interruption latency without requiring
provider token streaming.

## Phase 3 Relationship

Phase 3 should improve realtime output, not block Phase 2.

Phase 3 remains responsible for:

- Provider-level response delta support.
- Real `response.chunk` events from model deltas.
- First-token/first-chunk latency metrics.
- Fine-grained progress events for memory load, graph nodes, and provider calls.
- More precise cancellation latency metrics.
- Optional out-of-process realtime transport that depends on
  `RealtimeAgentBackend`, not Gateway/runTime internals.

Without Phase 3, interruption can still be semantically correct but may not feel
instant when the current provider call is blocking. With Phase 3, interruption
can become both semantically correct and perceptually responsive.

## Test Plan

Phase 2 tests are offline and deterministic.

Covered tests:

- `AgentGraphRealtimeBackend` passes `cancel_token` into
  `run_assistant_request(...)`.
- Pre-run cancellation still avoids calling the runner.
- Graph node boundary cancellation returns cancelled status.
- Cancellation before a tool call skips tool execution.
- Cancellation after a tool call prevents final response emission.
- `ToolContext` exposes cancellation to tools.
- `AgentGraphRuntime` pre-graph cancellation records run/session status as
  `cancelled` and emits no final response.
- Gateway `run.cancel` still emits `run.end reason=cancelled`.
- Same-session interrupt still cancels the previous run and starts the new run.
- `/agent/run` and existing WebSocket behavior remain unchanged when no cancel
  token is provided.
- No imports from `openclaw_gateway_runtime` are introduced.

Recommended validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_runtime_cancellation.py \
  tests/test_tool_executor.py \
  tests/test_realtime_agent_backend.py \
  tests/test_realtime_event_mapping.py \
  tests/test_realtime_backend_types.py \
  tests/test_gateway.py \
  tests/test_gateway_session.py
```

When implementation touches graph/tool internals, also run the relevant agent
and tool executor tests plus full pytest before merging.

## Acceptance Criteria

Phase 2 is complete when:

- Gateway cancellation reaches the assistant graph through a neutral token.
- The assistant graph can stop at node boundaries with a structured cancelled
  result.
- `ToolExecutor` can skip or stop work when cancellation is observed.
- `ToolContext` exposes cancellation without forcing every tool to become async.
- Provider/chat blocking calls remain timeout-protected and are checked before
  and after execution.
- Cancelled runs do not emit stale final response events after cancellation is
  observed.
- Existing non-realtime `/agent/run` and WebSocket paths are unchanged unless
  they explicitly pass a cancel token.
