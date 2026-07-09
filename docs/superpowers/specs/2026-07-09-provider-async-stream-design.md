# Provider Async Stream Design

Date: 2026-07-09

## Purpose

This design reviews Phase 6D of the runtime event-stream migration: whether to
add an async provider stream API after `LLMEvent` has been introduced.

The decision is to add only an optional provider-boundary async stream in a
future implementation phase. It must not replace the synchronous runtime,
change Gateway frames, or make Gateway consume provider events.

## Decision

Use the conservative async-boundary option:

```python
class AsyncStreamingChatAdapter(Protocol):
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        ...
```

This is an additive provider capability. `ChatAdapter.chat()` remains the main
compatibility contract, and `ChatResult` remains the terminal provider result
for existing runtime paths.

## Current Boundary

Current runtime-facing flow:

```text
Gateway / CLI / HTTP
  -> AgentGraphRuntime.run_stream()
  -> AgentEvent
  -> RealtimeAgentEvent / Gateway frame / CLI output
```

Current provider-facing flow:

```text
AgentGraphRuntime / assistant loop
  -> ChatAdapter.chat(ChatRequest)
  -> ChatResult
```

Current streaming compatibility flow:

```text
OpenAI-compatible chunk
  -> LLMEvent
  -> ChatRequest.stream_callback(text, payload)
  -> AgentEvent(response_delta)
```

Phase 6A through 6C improved the internal event boundary, but provider calls are
still invoked through the synchronous `chat()` method.

## Selected Design

Add an optional async stream interface at the provider boundary only.

The interface yields `LLMEvent` records:

- `token_delta` for model text deltas;
- `tool_call_delta` for streamed native tool-call fields;
- `completed` for finish reason, usage, and terminal metadata;
- `error` for prompt-safe provider errors.

The interface does not include `session_id`, `run_id`, Gateway frame names, TTS
fields, UI fields, raw provider chunks, API keys, or raw SDK responses.

Runtime and Gateway must continue to consume `AgentEvent`, not `LLMEvent`.

## Non-Goals

- Do not remove or rename `ChatAdapter.chat()`.
- Do not remove `ChatRequest.stream_callback`.
- Do not make all providers async.
- Do not make Gateway, TTS, UI, or realtime entry layers consume `LLMEvent`.
- Do not stream raw tool-call arguments to user-visible consumers.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not change memory policy, context rendering, or session history behavior.

## Rejected Options

### Stop After Phase 6C

Keeping only callbacks is stable, but leaves no provider-native async contract
for future streaming clients. This would keep future provider work coupled to
legacy `(text, payload)` callbacks.

### Rewrite Runtime Around Provider Async Streams Now

Making the assistant loop directly consume provider async streams is too broad
for this phase. It would touch tool execution, cancellation, final result
assembly, context handling, realtime backend behavior, and tests at the same
time.

## Data Flow

Future optional provider stream:

```text
OpenAI-compatible async provider stream
  -> AsyncStreamingChatAdapter.stream_chat()
  -> LLMEvent
```

Existing runtime stream remains:

```text
LLMEvent(token_delta)
  -> runtime mapper
  -> AgentEvent(response_delta)
  -> RealtimeAgentEvent(response.chunk)
  -> Gateway stream.chunk
```

The two streams have different scopes:

- `LLMEvent` is provider-internal.
- `AgentEvent` is runtime-internal and consumer-facing inside this project.
- `RealtimeAgentEvent` and Gateway frames are entry-layer contracts.

## Result Semantics

Event streams describe progress. Results describe terminal state.

`stream_chat()` should not replace `ChatResult` in existing callers. A future
consumer that uses `stream_chat()` and needs a terminal provider result must
accumulate events with `LLMEventAccumulator` or an equivalent helper and then
build a `ChatResult`.

The synchronous `chat()` method may continue to use its current parser and
callbacks. It does not need to be implemented in terms of `stream_chat()` in
the first implementation.

## Error Handling

Provider exceptions should be converted at the adapter boundary into
`LLMEvent(event_type="error", error=LLMProviderError(...))` when they are part
of normal provider failure behavior.

The async generator may still raise for programming errors, invalid adapter
state, or task cancellation. It must not leak raw SDK error objects or raw
provider response payloads to runtime consumers.

## Cancellation And Cleanup

The first implementation should rely on async generator cancellation semantics:
when the consumer exits early or the task is cancelled, the adapter must close
the underlying provider stream if the client exposes a close/aclose mechanism.

No new thread-shared cancellation flag should be introduced. Existing runtime
and Gateway cancellation still flows through the current cancel token boundary.

Provider-level hard cancellation can be evaluated later after the runtime has a
native async provider consumer.

## Backpressure

`stream_chat()` should be a pull-based async iterator. It should not introduce
an internal unbounded queue at the provider boundary.

Queue bridges remain acceptable at runtime facade boundaries where sync code is
still being adapted into async consumers.

## Initial Implementation Scope

The first implementation plan should be narrow:

1. Add `AsyncStreamingChatAdapter` as an optional protocol near the existing
   chat adapter contracts.
2. Add a mock `stream_chat()` implementation that emits deterministic
   `LLMEvent` records.
3. Add an OpenAI-compatible async stream implementation only if it can use the
   already-installed OpenAI package without adding dependencies.
4. Add tests that prove streamed `LLMEvent` records can be accumulated into the
   same terminal content/tool-call shape as the existing parser.
5. Keep `AgentGraphRuntime`, `run_assistant_request_stream()`, realtime backend,
   and Gateway unchanged.

## Testing Strategy

Focused provider tests should cover:

- mock async stream emits `token_delta` then `completed`;
- OpenAI-compatible async stream maps text deltas to `LLMEvent(token_delta)`;
- OpenAI-compatible async stream maps tool-call deltas to
  `LLMEvent(tool_call_delta)`;
- provider errors become `LLMEvent(error)` with prompt-safe fields;
- existing `ChatAdapter.chat()` tests still pass unchanged.

Runtime and Gateway tests should not need behavior changes for the first
implementation. The fast test suite should remain green.

## Acceptance Criteria

The Phase 6D implementation is acceptable only if:

- `ChatAdapter.chat()` remains available and compatible;
- `ChatResult` remains the terminal result for existing runtime paths;
- Gateway frame names and realtime event types do not change;
- `LLMEvent` does not cross into Gateway, TTS, UI, or public API contracts;
- no provider raw chunks are exposed outside provider adapters;
- no broad async rewrite of tool, memory, runtime, or Gateway code is included.

## Review

This design is intentionally not a runtime rewrite. It creates a low-risk
provider async stream boundary that can later be consumed by the runtime when the
runtime loop is ready to become async-native.
