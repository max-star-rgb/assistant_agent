# Runtime Thread Model Audit

Last updated: 2026-07-09

This audit records Phase 5 of the runtime event-stream migration. The goal is
to decide where threads are still the right boundary, where async should become
native later, and where no migration is justified yet.

This is intentionally an architecture assessment. It does not remove
`asyncio.to_thread()` or rewrite tools, memory stores, or provider adapters.

## Summary

The current runtime is not using a broad thread-pool architecture. The core
thread bridge is narrow and deliberate:

```text
Gateway / realtime async code
  -> run_assistant_request_stream()
  -> asyncio.to_thread(run_assistant_request)
  -> AgentGraphRuntime.run_state()
  -> EventSink.emit(AgentEvent)
  -> AsyncQueueEventSink
  -> async iterator
```

There are three production `asyncio.to_thread()` bridges:

- `AgentGraphRuntime.run_stream()` wraps synchronous `run_state()`.
- `run_assistant_request_stream()` wraps synchronous `run_assistant_request()`.
- `AgentGraphRealtimeBackend` keeps a compatibility wrapper for injected
  synchronous `run_request=` callables.

Those bridges are acceptable for the current stage. They expose a first-class
async stream interface without changing the synchronous source of truth.

The next valuable async work is not generic thread removal. The next valuable
work is to make the LLM streaming provider boundary event-native, while keeping
blocking tools and side-effecting provider SDKs behind governed sync boundaries
until evidence shows they are bottlenecks.

## Classification

| Area | Current model | Classification | Rationale |
| --- | --- | --- | --- |
| `AgentGraphRuntime.run_stream()` | `asyncio.to_thread(run_state)` plus `AsyncQueueEventSink` | Keep short-term | `run_state()` still owns graph execution, terminal state, history, trace, and event emission. The facade is the right migration boundary. |
| `run_assistant_request_stream()` | `asyncio.to_thread(run_assistant_request)` | Keep short-term | The service owns env resolution, runtime creation, conversation history, realtime task state, demo video preloading, and `AssistantRunArtifacts`. |
| Realtime backend default path | consumes `run_assistant_request_stream()` with `async for` | Good async boundary | Gateway now pulls `AgentEvent` records instead of registering callback bridges. |
| Realtime backend sync injection | compatibility stream wrapper around `run_request=` | Keep compatibility only | Useful for tests and legacy adapters. New production code should prefer `run_request_stream=`. |
| Gateway sessions and bridge | native `asyncio` tasks, locks, deadlines, websocket transport | Keep async | This layer is naturally async and owns connection lifecycle, cancel, disconnect, and backpressure-facing behavior. |
| `ToolExecutor` retry sleep | synchronous short sleep chunks with cancel checks | Keep short-term | It runs inside the sync runtime worker. Phase 4 made it cooperative enough without changing tool contracts. |
| Tool registry and tool functions | synchronous governed calls | Keep by default | Tool calls must remain behind validator, executor, registry, policy, audit, and memory identity boundaries. Async conversion should be per tool, not global. |
| OpenAI-compatible chat adapter | synchronous SDK call and callback streaming | First async-native candidate | This is the high-value streaming path. It should evolve toward provider-neutral `LLMEvent` output without leaking vendor chunks. |
| HTTP web/product/image/vision adapters | synchronous SDK or `urllib` calls | Keep behind sync/thread boundary | These are blocking external provider calls with timeout/error policy and artifacts. Async migration is useful only if measured latency or concurrency requires it. |
| Memory stores and local services | synchronous local I/O and small locks | Keep | Local JSON/SQLite/in-memory behavior is not the current bottleneck. Async DB work would add complexity without improving the event-stream boundary. |
| In-process locks | `RLock`, `threading.Lock`, `asyncio.Lock` depending on layer | Keep | Locks are scoped to process-local maps or async session state. They are not a separate runtime thread model. |
| Scripts and smoke clients | subprocess/websocket/http clients | Out of runtime core | Do not use script-only blocking behavior to justify runtime architecture changes. |

## What Must Stay Threaded For Now

Some work is still better isolated behind the synchronous worker boundary:

- blocking SDKs without a reliable async client;
- `urllib` provider calls and artifact downloads;
- subprocess-backed or shell-oriented tools;
- filesystem-heavy local artifact work;
- legacy libraries that are already covered by timeout, policy, and audit
  boundaries;
- short CPU-light local transforms where async would only add ceremony.

These are not architectural failures. They are practical compatibility
boundaries. The important requirement is that they remain governed and bounded
by timeout, budget, cancellation checks where possible, and structured errors.

## What Should Become Async First

The best async-native candidate is LLM streaming.

Current shape:

```text
provider chunk
  -> ChatRequest.stream_callback(text, payload)
  -> AgentEvent(type="response_delta")
  -> EventSink
  -> async stream facade
```

Target direction:

```text
provider chunk
  -> provider-neutral LLMEvent
  -> AgentEvent
  -> async stream
```

The runtime consumer should still never see OpenAI, Anthropic, Qwen, DeepSeek,
Codex, or other vendor-specific chunks. Provider adapters translate vendor data
into a small internal event contract first.

The first async-native provider migration should focus on text deltas and
tool-call deltas from the chat adapter. That path benefits Gateway, TTS, UI, and
logging consumers immediately.

## Cancellation Reality

Phase 4 improved cooperative cancellation, but it did not make blocking calls
preemptive.

Current guarantees:

- Gateway cancellation is represented by an event-like token.
- Runtime and tool boundaries call `raise_if_cancelled()`.
- Tool retry backoff wakes in short chunks and rechecks cancellation.
- The async stream facade publishes cancellation events produced by the sync
  runtime.

Current limitations:

- A blocking provider SDK call may not stop until its timeout or response.
- A blocking tool may not stop until it reaches a cooperative check.
- `asyncio.to_thread()` cannot forcibly kill a worker thread safely.

This is acceptable for the current architecture. Future work should reduce the
highest-value blocking provider path first, not try to force-cancel arbitrary
sync tools.

## Migration Rules

Use these rules before converting a component to async:

1. Convert only if the component is on a high-frequency streaming or
   connection-lifecycle path.
2. Keep provider-specific raw events inside provider adapters.
3. Keep tool calls behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
4. Keep memory policy behind memory services and memory tools.
5. Keep sync adapters when the upstream library is sync-only or artifact-heavy.
6. Do not add async variants that duplicate business logic without removing
   meaningful blocking, callback, or lifecycle complexity.

## Recommended Next Step

Phase 6 should be a provider-boundary design pass, not another broad runtime
rewrite.

Recommended scope:

- define a provider-neutral `LLMEvent` contract;
- map existing chat stream callbacks into `LLMEvent` without changing public
  provider behavior;
- decide how `LLMEvent` becomes `AgentEvent`;
- keep `ChatAdapter.chat()` compatible while adding a narrow async stream path
  only where tests can prove consumer behavior improves.

Stop here before making tools or memory async. The runtime now has the right
outer async interface. The next useful work is to make the highest-value
producer path native to that interface.
