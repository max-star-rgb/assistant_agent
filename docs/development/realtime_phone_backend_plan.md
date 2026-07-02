# Realtime Phone Backend Plan

## Background

`assistant_agent` is the business Agent brain. Its current core includes LangGraph, `AgentGraphRuntime`, `ToolRegistry`, `ToolExecutor`, Memory, Trace, provider adapters, and final response composition.

`runTime` or another phone runtime is the realtime outer runtime. It owns external connections, call lifecycle, session/run lifecycle, cancellation, interruption, stream frames such as `stream.chunk`, terminal frames such as `run.end`, and TTS-friendly delivery.

The goal is to add a realtime backend capability inside `assistant_agent` that can be called by a phone runtime without coupling `assistant_agent` to that runtime's concrete protocol. The integration must avoid stacking two Agent loops. In particular, an OpenClaw Anthropic `tool_use` loop must not wrap the `assistant_agent` LangGraph loop.

## Goals

- Add a neutral `RealtimeAgentBackend` boundary inside `assistant_agent`.
- Keep `assistant_agent` responsible for business Agent orchestration, tool selection, Memory, Trace, and final response generation.
- Allow a phone runtime to call `assistant_agent` through a thin adapter layer.
- Keep the adapter/backend layer limited to protocol conversion, event conversion, identity pass-through, cancellation bridging, and error mapping.
- Preserve existing `/agent/run` and `/ws/agent/{session_id}` behavior.
- Provide an MVP that emits tool/progress/final-response events and chunks the final response text for realtime delivery.
- Make cancellation behavior explicit as best-effort in the MVP.

## Non-goals

- Do not modify `runTime` as part of the `assistant_agent` MVP.
- Do not make `assistant_agent` import `openclaw_gateway_runtime`.
- Do not use `OpenClawAdapter`, `AdapterEvent`, or runTime `Frame` as formal internal abstractions in `assistant_agent`.
- Do not introduce an OpenClaw Anthropic `tool_use` loop around the `assistant_agent` LangGraph loop.
- Do not promise token-level streaming in the MVP.
- Do not promise hard cancellation in the MVP.
- Do not replace `/agent/run`, `/agents/run`, or `/ws/agent/{session_id}`.
- Do not move call session ownership, TTS pacing, or external stream-frame protocols into `assistant_agent`.

## Ownership boundaries

The phone runtime owns:

- External WebSocket or telephony connection management.
- `call.incoming`, `call.ready`, `call.hangup`, reconnect, and client disconnect behavior.
- Runtime-side `session_id`, `turn_id`, and `run_id` lifecycle.
- `run.cancel` handling and same-session interruption.
- Stream-frame delivery such as `stream.chunk` and terminal frames such as `run.end`.
- TTS-friendly pacing, display-only content handling, and client protocol compatibility.

`assistant_agent` owns:

- `UserRequest` to `AgentGraphRuntime` execution.
- LangGraph assistant loop and plan/tool orchestration.
- Business tool selection through `ToolRegistry` and tool execution through `ToolExecutor`.
- Memory loading, memory writes, conversation context, and Memory audit behavior.
- Trace creation and queryable trace storage.
- Final `AgentResponse` composition.
- Provider selection, provider budgets, and provider/tool error normalization.

The realtime backend owns only the boundary between those concerns:

- Convert `RealtimeAgentRequest` into `UserRequest`.
- Convert `AgentEvent` into `RealtimeAgentEvent`.
- Convert `AgentResponse` and `AgentState` into `RealtimeAgentResult`.
- Bridge cancellation signals on a best-effort basis.
- Map internal errors to realtime backend result/error events.

## Proposed architecture

Add a small neutral realtime package under `assistant_agent`:

- `src/assistant_agent/realtime/types.py`
- `src/assistant_agent/realtime/backend.py`
- `src/assistant_agent/realtime/agent_graph_backend.py`

The package should not import `openclaw_gateway_runtime`.

Expected runtime composition:

```text
phone runtime
  -> phone-runtime-specific adapter
     -> assistant_agent.realtime.RealtimeAgentBackend
        -> run_assistant_request(...)
           -> AgentGraphRuntime
              -> LangGraph / ToolRegistry / ToolExecutor / Memory / Trace
```

The phone-runtime-specific adapter may live outside `assistant_agent`, or in a separate integration package. It can adapt a runtime-specific interface to `RealtimeAgentBackend`, but runtime-specific types must not become `assistant_agent` core types.

## New interfaces

The following draft is intentionally neutral. Names and fields can be refined during implementation, but the dependency direction must stay the same.

```python
class RealtimeCancelToken(Protocol):
    def is_cancelled(self) -> bool: ...
    async def cancelled(self) -> None: ...
```

```python
class RealtimeAgentRequest(BaseModel):
    user_id: str
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    text: str
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

```python
class RealtimeAgentEvent(BaseModel):
    type: Literal[
        "run.progress",
        "tool.started",
        "tool.finished",
        "tool.failed",
        "trace.decision",
        "trace.observation",
        "response.chunk",
        "response.final",
        "error",
    ]
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    display_only: bool = False
    content_type: str = "text"
```

```python
class RealtimeAgentResult(BaseModel):
    status: Literal["completed", "cancelled", "error"]
    response_text: str = ""
    expects_reply: bool = False
    run_id: str | None = None
    trace_id: str | None = None
    output_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

```python
class RealtimeBackendCapabilities(BaseModel):
    supports_token_streaming: bool = False
    supports_tool_event_streaming: bool = True
    supports_best_effort_cancel: bool = True
    supports_hard_cancel: bool = False
    supports_multimodal_refs: bool = True
```

```python
class RealtimeAgentBackend(Protocol):
    @property
    def capabilities(self) -> RealtimeBackendCapabilities: ...

    async def run_turn(
        self,
        request: RealtimeAgentRequest,
        *,
        event_sink: Callable[[RealtimeAgentEvent], Awaitable[None]] | None = None,
        cancel_token: RealtimeCancelToken | None = None,
    ) -> RealtimeAgentResult: ...
```

MVP capability values should be explicit:

- `supports_token_streaming = False`
- `supports_tool_event_streaming = True`
- `supports_best_effort_cancel = True`
- `supports_hard_cancel = False`

## MVP scope

Add `AgentGraphRealtimeBackend` in `src/assistant_agent/realtime/agent_graph_backend.py`.

MVP behavior:

- Convert `RealtimeAgentRequest` to existing `UserRequest`.
- Preserve `user_id`, `session_id`, `run_id`, `turn_id`, and runtime metadata in `UserRequest.metadata`.
- Call existing `run_assistant_request(...)`.
- Use a custom `EventSink` to map `AgentEvent` to `RealtimeAgentEvent`.
- Keep existing `/agent/run` and `/ws/agent/{session_id}` unchanged.
- Do not change `AgentGraphRuntime` signatures in MVP unless strictly necessary.

Event mapping:

- `tool_started` -> `tool.started`
- `tool_finished` / `tool_completed` -> `tool.finished`
- `tool_failed` -> `tool.failed`
- `agent_trace_decision` -> `trace.decision`
- `agent_trace_observation` -> `trace.observation`
- `final_response` -> `response.final`

Response chunking:

- After `run_assistant_request(...)` returns, split final `AgentResponse.message` into `response.chunk` events.
- Chunking may be sentence-based or bounded-length text chunking.
- Chunks are not token-level streaming and must not be documented as model streaming.
- Emit `response.final` after chunk emission if it has not already been emitted.

Cancellation:

- Check `cancel_token.is_cancelled()` before starting the run.
- Check `cancel_token.is_cancelled()` after the run completes.
- If cancelled before start, return `RealtimeAgentResult(status="cancelled")`.
- If cancelled during a synchronous run, mark result as best-effort. The MVP does not guarantee immediate interruption.

MVP tests:

- text-only request returns a completed result.
- tool lifecycle `AgentEvent` maps to realtime tool events.
- final response maps to `response.final`.
- final `AgentResponse.message` is emitted as `response.chunk`.
- tool failure maps to `tool.failed`.
- backend error maps to an `error` event and `RealtimeAgentResult(status="error")`.
- pre-run cancellation returns `RealtimeAgentResult(status="cancelled")`.

Suggested test file:

- `tests/test_realtime_agent_backend.py`

## Phase 2

Add cooperative cancellation deeper in `assistant_agent`.

Potential changes:

- Extend runtime-only graph context with a neutral cancel token.
- Add optional cancel checks at graph node boundaries.
- Add optional cancel checks before and after `ToolExecutor.run_tool(...)`.
- Add cancel awareness to `ToolContext`.
- Add a cancellable or async chat boundary alongside the current synchronous `ChatAdapter`.
- Return a structured cancelled result when cancellation is observed.

Important constraint:

- Do not replace existing `ChatAdapter.chat(...)` callers unless compatibility is preserved.
- Do not break existing HTTP and WebSocket endpoints.

## Phase 3

Converge Gateway output gating and lightweight realtime semantics after the
neutral backend is stable.

Required behavior:

- Keep one run-scoped cancel token owned by the Gateway/session layer.
- When `run.cancel`, same-session interrupt, or deadline expiry cancels that
  token, immediately stop forwarding queued or late outbound events for the old
  run.
- Drop old-run `response.chunk`, `response.final`, tool, trace, and error events
  after cancellation is observed at the Gateway boundary.
- Still emit the old run's terminal `run.end` frame with
  `reason="cancelled"` and `expects_reply=true`.
- Let the backend/agent finish current synchronous provider or tool work
  naturally; Phase 3 does not hard-cancel provider calls.
- Preserve deadline diagnostics through cancel metadata such as
  `cancel_source="deadline"`, `cancel_reason="run_deadline_expired"`, and
  `deadline_ms`.

Optional future improvements:

- Add provider-level response delta support when provider adapters expose it.
- Emit real `response.chunk` events from model deltas when provider support exists.
- Add richer realtime progress events for memory load, graph nodes, and provider calls.
- Add multimodal realtime request mapping for image/video/audio references.
- Add optional out-of-process transport that still depends on `RealtimeAgentBackend`, not runTime internals.
- Add metrics for realtime backend latency, event counts, cancellation timing, and degradation mode.

## Risks and degradation strategy

Risk: MVP is not token streaming.

- Degrade by emitting tool/progress events during execution and chunking the final response after completion.
- Clearly expose `supports_token_streaming = False`.

Risk: MVP cancellation is not hard cancellation.

- Degrade by checking cancellation before and after the run.
- Clearly expose `supports_hard_cancel = False`.
- Use provider/tool timeouts for blocking calls.

Risk: two session/history systems can conflict.

- Treat the phone runtime session as transport lifecycle.
- Treat `assistant_agent` conversation history, Memory, and Trace as business state.
- Pass runtime identifiers as metadata without making runtime history authoritative.

Risk: runtime-specific protocol leaks into `assistant_agent`.

- Keep runtime adapters outside the internal backend interface.
- Reject imports from `openclaw_gateway_runtime` in `assistant_agent` implementation.

Risk: tool events are not TTS friendly by default.

- Mark tool/progress/trace events as display-only at adapter level if needed.
- Keep spoken output focused on `response.chunk`.

Risk: existing endpoints regress.

- Add tests around `/agent/run` and `/ws/agent/{session_id}` behavior.
- Keep the realtime package additive.

## Testing plan

Unit tests:

- `RealtimeAgentRequest` to `UserRequest` mapping.
- `AgentEvent` to `RealtimeAgentEvent` mapping.
- `AgentResponse.message` chunking.
- pre-run cancellation.
- post-run best-effort cancellation metadata.
- error mapping.
- capabilities defaults.

Integration tests:

- `AgentGraphRealtimeBackend` with mock providers.
- text-only run emits chunks and a completed result.
- tool run emits tool lifecycle events and final response events.
- failed tool run emits `tool.failed` and a final error-aware result.
- existing `run_assistant_request(...)` path still works.
- existing `/agent/run` behavior remains unchanged.
- existing `/ws/agent/{session_id}` behavior remains unchanged.

Regression checks:

- No import of `openclaw_gateway_runtime` under `src/assistant_agent`.
- No changes required in `runTime`.
- No replacement of existing `AgentGraphRuntime.run_state(...)` behavior in MVP.

## Acceptance criteria

MVP is accepted when:

- `src/assistant_agent/realtime/types.py` exists with neutral realtime request/event/result/capability types.
- `src/assistant_agent/realtime/backend.py` exists with `RealtimeAgentBackend`.
- `src/assistant_agent/realtime/agent_graph_backend.py` exists with `AgentGraphRealtimeBackend`.
- `AgentGraphRealtimeBackend` delegates to `run_assistant_request(...)`.
- `AgentGraphRealtimeBackend` does not import `openclaw_gateway_runtime`.
- `OpenClawAdapter`, `AdapterEvent`, and runTime `Frame` are not internal `assistant_agent` abstractions.
- Existing `/agent/run` behavior is unchanged.
- Existing `/ws/agent/{session_id}` behavior is unchanged.
- MVP emits mapped tool events.
- MVP emits final response chunks.
- MVP reports best-effort cancellation honestly.
- Tests cover text-only, tool event, final response, error mapping, and pre-run cancellation behavior.
