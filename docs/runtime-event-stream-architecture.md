# Runtime Event Stream Architecture

Last updated: 2026-07-09

This document defines the target direction for evolving `assistant_agent` runtime events from a synchronous callback side channel into an async event stream interface. It is intentionally incremental. The current Python runtime, Gateway, tool governance, memory service, provider adapters, and realtime contracts remain authoritative.

## Current State

The current execution path for Gateway-normalized traffic is:

```text
GatewaySessionService
  -> AgentGraphRealtimeBackend
  -> asyncio.to_thread(run_assistant_request)
  -> AgentGraphRuntime.run_state()
  -> EventSink.emit(AgentEvent)
  -> AgentEvent -> RealtimeAgentEvent -> Gateway frame
```

The repository already has the important runtime pieces:

- `assistant_agent.schemas.events.AgentEvent` for internal runtime events.
- `assistant_agent.services.event_sink.EventSink` for synchronous event delivery.
- `assistant_agent.realtime.RealtimeAgentEvent` for the Gateway backend boundary.
- `assistant_agent.realtime.event_mapping` for `AgentEvent` to `RealtimeAgentEvent` conversion.
- `assistant_agent.gateway.session.CancelToken`, backed by `asyncio.Event`, for Gateway cancellation.
- `AgentGraphRealtimeBackend`, which is the thin bridge between Gateway and the existing assistant runtime.

The current event system is useful, but it is still an observer-style side channel. Runtime callers register or pass an `EventSink`, and the runtime pushes events into it while the final result is returned separately through `run_state()`, `run()`, or `run_assistant_request()`.

## Problem

The existing observer model makes streaming work, but it keeps event delivery separate from the runtime output protocol.

This creates several long-term issues:

- Consumers need callback-style registration instead of `async for` consumption.
- Streaming and final-result handling are easy to couple incorrectly.
- Backpressure and lifecycle ownership are unclear at async boundaries.
- Exceptions and terminal state need custom bridges when sync runtime execution is called from async Gateway code.
- The realtime backend currently bridges sync events back into the event loop with thread coordination.

The next architectural step is not to redesign events. The next step is to expose existing `AgentEvent` records as a first-class async stream while preserving final result contracts.

## Goals

- Add an async stream facade over the existing runtime event system.
- Reuse the existing `AgentEvent` schema and event names.
- Preserve `AgentGraphRuntime.run_state()` and `AgentGraphRuntime.run()` behavior.
- Preserve `run_assistant_request()` and `AssistantRunArtifacts` behavior.
- Preserve `RealtimeAgentRequest`, `RealtimeAgentEvent`, `RealtimeAgentResult`, `RealtimeAgentBackend`, and `RealtimeCancelToken`.
- Preserve Gateway wire frame names and lifecycle semantics.
- Keep all tool calls behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Keep memory behavior behind `MemoryManager`, memory services, and memory tools.
- Keep provider-specific chunks out of runtime consumers.

## Non-Goals

- Do not replace `AgentGraphRuntime` with a second agent loop.
- Do not introduce a third event schema for runtime events.
- Do not rename current `AgentEvent` types in the first phase.
- Do not migrate all tools, memory stores, or provider adapters to async in the first phase.
- Do not change Gateway frame names or public Gateway behavior.
- Do not move planning, tool choice, memory policy, provider policy, or agent routing into Gateway.
- Do not make TTS, UI, or transport-specific concepts part of `AgentEvent`.

## Event Versus Result

Runtime event streams and final results are separate concepts.

An event describes what happened during a run:

```text
task_started
graph_node_started
response_delta
tool_started
tool_finished
final_response
task_cancelled
task_failed
```

A result describes the terminal outcome of the run:

```text
AgentState
AssistantRunArtifacts
RealtimeAgentResult
```

The stream interface must not force callers to reconstruct all terminal metadata from events. Callers that need final status, trace id, output refs, conversation-history effects, realtime task-state effects, or Gateway `expects_reply` behavior must still have an explicit result path.

## Phase 1 Target

Phase 1 adds a runtime-level async stream facade only:

```python
stream = runtime.run_stream(request, cancel_token=cancel_token)

async for event in stream:
    consume(event)

state = await stream.result()
```

The stream yields existing `AgentEvent` objects. The terminal result remains `AgentState`.

Internally, Phase 1 may still call the existing synchronous `run_state()` implementation in a worker thread. A thread-safe event sink bridges `EventSink.emit()` into the owning event loop:

```text
AgentGraphRuntime.run_state()
  -> EventSink.emit(AgentEvent)
  -> loop.call_soon_threadsafe(queue.put_nowait, item)
  -> AgentRunStream.__anext__()
  -> AgentEvent
```

This is an async facade, not a native async runtime. The facade changes the consumer interface without changing the internal execution model.

## Phase 1 Interfaces

The first implementation should introduce a small runtime stream module:

```text
src/assistant_agent/agent/event_stream.py
```

Expected public objects:

```python
class AgentRunStream:
    def __aiter__(self) -> AgentRunStream: ...
    async def __anext__(self) -> AgentEvent: ...
    async def result(self) -> AgentState: ...
    async def wait(self) -> AgentState: ...


class AsyncQueueEventSink:
    def emit(self, event: AgentEvent) -> None: ...
```

`AgentGraphRuntime` then exposes:

```python
def run_stream(
    self,
    request: UserRequest,
    *,
    event_sink: EventSink | None = None,
    cancel_token: Any | None = None,
) -> AgentRunStream:
    ...
```

## Phase 1 Implemented Interfaces

`AgentGraphRuntime.run_stream(request, *, event_sink=None, cancel_token=None)`
returns `AgentRunStream[AgentState]`.

`AgentRunStream` supports async iteration over `AgentEvent` and
`await stream.result()` for the terminal `AgentState`.

The optional `event_sink` remains useful for compatibility observers. The stream sink should forward to that sink after enqueuing or before enqueuing, as long as order is deterministic and sink failures cannot leave the stream hanging.

## Threading Model

`asyncio.Queue` is not thread-safe. If the synchronous runtime runs in `asyncio.to_thread()`, `EventSink.emit()` may be called from a worker thread.

The bridge must schedule queue writes on the owning event loop:

```python
loop.call_soon_threadsafe(queue.put_nowait, item)
```

The worker thread must never call `asyncio.Queue.put_nowait()` directly.

The bridge must also publish terminal completion through the event loop. The async iterator should finish only after:

- all previously emitted events are available to the consumer, and
- the worker has stored either a final `AgentState` or an exception.

## Error And Cancellation Semantics

Phase 1 reuses the existing cancellation path:

- Gateway owns `CancelToken`.
- `AgentGraphRuntime.run_state()` receives the cancel token.
- Runtime graph nodes and `ToolExecutor` continue checking cancellation through existing helpers.
- Cancellation still produces `task_cancelled` where the current runtime already emits it.

The stream facade should not invent new interrupt semantics.

If the worker raises an unexpected exception before producing an `AgentState`, `AgentRunStream.result()` should re-raise that exception. The async iterator may also re-raise after queued events are drained. Tests should lock this behavior before implementation.

## Phase 2 Service-Level Stream Facade

Phase 2 adds `run_assistant_request_stream()` on top of the shared assistant run
service:

```python
stream = run_assistant_request_stream(request, ...)

async for event in stream:
    consume(event)

artifacts = await stream.result()
```

The stream yields existing `AgentEvent` objects. The terminal result remains
`AssistantRunArtifacts`.

This phase deliberately keeps `run_assistant_request()` as the synchronous
source of truth because it owns more than raw runtime execution:

- env and provider config resolution
- runtime creation
- conversation history preparation
- realtime task-state request preparation
- realtime task-state event reduction
- demo video context preload
- final conversation turn recording
- `AssistantRunArtifacts`

Internally, Phase 2 still runs the existing service function in a worker thread
and bridges `EventSink.emit()` into an async iterator. This is a service-level
async facade, not a native async runtime. It changes the consumer boundary while
preserving the current run service behavior and final-result contract.

`AgentGraphRealtimeBackend` should migrate to the service-level stream facade,
not directly around `run_assistant_request()` to construct `AgentGraphRuntime`
itself.

## Phase 2 Implemented Interfaces

`run_assistant_request_stream(request, **kwargs)` accepts the same operational
inputs as `run_assistant_request()` and returns
`AgentRunStream[AssistantRunArtifacts]`.

The facade forwards events to an optional compatibility `event_sink` while also
recording them for `AssistantRunArtifacts.events`. This keeps callback-style
observers working during migration and lets new consumers use:

```python
stream = run_assistant_request_stream(request, event_sink=legacy_sink)

async for event in stream:
    consume(event)

artifacts = await stream.result()
```

## Phase 3 Realtime Backend Migration

Phase 3 moves `AgentGraphRealtimeBackend` from this shape:

```text
asyncio.to_thread(run_assistant_request)
  + _RealtimeForwardingEventSink.emit()
  + asyncio.run_coroutine_threadsafe(...)
```

to this shape:

```text
run_assistant_request_stream()
  -> async for AgentEvent
  -> map AgentEvent to RealtimeAgentEvent
  -> await event_sink(RealtimeAgentEvent)
  -> await stream.result()
  -> RealtimeAgentResult
```

The backend now consumes `run_assistant_request_stream()` by default. Existing
sync `run_request=` injection remains supported through a compatibility stream
wrapper so API adapters and focused tests can keep passing a synchronous run
function during migration.

This migration preserves:

- progress throttling and heartbeat policy
- final response chunking behavior
- duplicate `response_delta` suppression
- cancel metadata and best-effort cancel status
- trace id propagation
- `expects_reply`
- stale event suppression after Gateway cancel or interrupt

## Phase 4 Cancellation Propagation

Phase 4 tightens cooperative cancellation without changing Gateway wire frames
or rewriting runtime internals.

The cancellation model remains:

```text
Gateway CancelToken / event-like token
  -> run_assistant_request_stream(..., cancel_token=...)
  -> AgentGraphRuntime.run_state(..., cancel_token=...)
  -> raise_if_cancelled()
  -> ToolExecutor / runtime node checks
```

Implemented Phase 4 changes:

- `raise_if_cancelled()` now recognizes event-like tokens with `is_set()`, so
  raw `asyncio.Event` style cancel tokens are treated as cancelled when set.
- `ToolExecutor` retry backoff no longer sleeps as one uninterruptible block.
  It sleeps in short chunks and checks the cancel token between chunks.

This phase does not forcefully terminate blocking external SDK calls,
subprocesses, or provider requests that do not cooperate. Those remain thread
or adapter-level migration candidates and should be handled only where evidence
shows a real bottleneck.

## Provider Event Direction

Provider-native streaming should eventually move from provider callbacks:

```text
provider chunk -> stream_callback(text, payload)
```

toward provider-neutral events:

```text
provider chunk -> LLMEvent -> AgentEvent -> async stream
```

That is not Phase 1. Provider adapter boundaries should continue hiding OpenAI, Anthropic, DeepSeek, Codex, or other vendor-specific chunks from the runtime consumer.

## Selective Async Direction

The long-term goal is selective async, not async everywhere.

Good candidates for native async:

- HTTP provider clients that support async streaming
- WebSocket transports
- realtime Gateway and media-entry adapters
- TTS or UI consumers that naturally consume text deltas

Good candidates to keep behind thread or sync boundaries:

- blocking SDKs
- subprocess-backed tools
- local filesystem work
- legacy libraries without reliable async APIs
- short CPU-light local transforms

## Acceptance Criteria

Phase 1 is complete only when:

- `AgentGraphRuntime.run_stream()` exists and returns an async-iterable stream handle.
- `async for event in runtime.run_stream(request)` yields existing `AgentEvent` records in runtime order.
- `await stream.result()` returns the same terminal `AgentState` that `run_state()` would return.
- `run()` and `run_state()` behavior is unchanged.
- `response_delta` and `final_response` are not duplicated beyond current runtime behavior.
- pre-run cancellation yields the same cancelled state and `task_cancelled` event behavior as `run_state()`.
- worker-thread event delivery uses `loop.call_soon_threadsafe`.
- no Gateway wire frame, realtime type, tool, memory, or provider schema changes are required.

## Validation

For Phase 1 runtime facade work:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py tests/test_agent_events.py tests/test_agent_runtime_cancellation.py -q
```

Before migrating the realtime backend:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
```

Before changing Gateway-facing behavior:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py -q
```
