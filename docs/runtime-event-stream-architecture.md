# Runtime Event Stream Architecture

Last updated: 2026-07-14

This document is the current authority for provider and assistant runtime
streaming in `assistant_agent`. It defines the event contracts, stream/result
separation, thread bridge, cancellation limits, compatibility boundaries, and
source ownership that are implemented today. Gateway session and wire-frame
lifecycle remain authoritative in `docs/gateway-architecture.md`.

## Scope And Invariants

The streaming stack has four distinct contracts:

```text
vendor provider chunks
  -> provider adapter -> LLMEvent
  -> runtime mapping and lifecycle -> AgentEvent
  -> AgentRunStream / shared assistant service stream
  -> RealtimeAgentEvent
  -> Gateway frame
```

- `LLMEvent` is provider-neutral and internal to the chat/provider boundary.
- `AgentEvent` describes assistant runtime progress and lifecycle.
- `RealtimeAgentEvent` is the thin realtime backend boundary.
- Gateway frames describe session, run, delivery, cancel, interrupt, and
  transport lifecycle.
- Vendor chunks, SDK objects, prompts, credentials, and raw provider responses
  do not cross the provider adapter boundary.
- Gateway, UI, TTS, and public API consumers do not consume `LLMEvent`.
- Provider streaming never bypasses tool governance. Final native tool calls
  still enter `ActionValidator -> ToolExecutor -> ToolRegistry`.

## Provider Stream Boundary

`AsyncStreamingChatAdapter.stream_chat()` is an optional additive interface.
Implementations normalize vendor chunks into these `LLMEvent` variants:

| event | purpose | terminal |
| --- | --- | --- |
| `token_delta` | prompt-safe response text progress | no |
| `tool_call_delta` | accumulated native tool-call name and arguments | no |
| `completed` | finish reason, usage, and stream completion | yes |
| `error` | prompt-safe provider failure | yes |

`LLMEventAccumulator` reconstructs response text, tool calls, finish reason,
usage, provider, and model into the existing terminal `ChatResult` contract.
Tool-call argument deltas are not exposed as user-visible response events.
Provider errors become structured `ChatResult.errors`; cancellation exceptions
remain cancellation signals rather than provider errors.
If a provider stream ends with `completed` but no text, tool calls, or refusal,
the runner normalizes it to `provider_empty_response` so sync and streaming chat
paths share the same empty-output contract.

For the main foreground chat LLM only, `provider_timeout` and
`provider_empty_response` with no usable text/tool/refusal are treated as a
recoverable no-answer condition. The runtime records the structured provider
diagnostic in state metadata, response data, and trace events, but completes the
run with an honest user-visible retry prompt instead of emitting `task_failed`:
`provider_timeout` reports `抱歉，刚才主模型没有及时响应，请再说一遍。`, and
`provider_empty_response` reports `抱歉，刚才主模型返回为空，请再说一遍。`.
Tool providers, vision/search providers, durable-task provider calls, and
cancellation paths do not use this fallback.

Foreground provider turns are consumed inside the shared LangGraph assistant
loop. Qwen chat defaults to Provider token streaming. Set both
`CHAT_STREAM=false` and `MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING=false` to
opt out unambiguously: the first controls synchronous SDK stream aggregation,
while the second controls the runtime's async-native stream consumer. Other providers remain opt-in through
`ProviderConfig.native_provider_streaming`. When enabled and the adapter exposes
`stream_chat()`, `ProviderStreamingTurnRunner` consumes the async stream for one
runtime turn. Visible token deltas pass through the existing stream callback and
`llm_event_mapping` to become `AgentEvent(type="response_delta")`. When the flag
is disabled or the adapter is sync-only, the runtime continues to call
`ChatAdapter.chat()`.

Every foreground assistant-loop Provider turn emits a paired
`llm.chat.started` / `llm.chat.finished` span. The finished event records bounded
provider/model labels, iteration, result kind, tool-call count, Provider-reported
latency, wall latency and normalized token usage; it never records prompt or
response content. Agent-Service latency summaries use wall latency as the
critical-path `llm_chat[n]` duration and keep Provider latency as a nested
diagnostic.

The compatibility contracts remain supported:

- `ChatAdapter.chat(request) -> ChatResult` remains valid.
- `ChatRequest.stream_callback(text, payload)` remains valid.
- OpenAI-compatible synchronous parsing still accumulates the same final
  `ChatResult` while adapting internal token events to the callback.
- Runtime callback sites normalize deltas through `stream_delta_to_agent_event`
  so provider metadata and runtime-owned `source` values stay consistent.

## Runtime Stream And Result

Events report what happened during a run; results report its terminal outcome.
They are intentionally separate:

| stream level | yielded events | terminal result |
| --- | --- | --- |
| provider turn | `LLMEvent` | `ChatResult` |
| graph runtime | `AgentEvent` | `AgentState` |
| shared assistant service | `AgentEvent` | `AssistantRunArtifacts` |
| realtime backend | `RealtimeAgentEvent` | `RealtimeAgentResult` |

Callers must not reconstruct terminal state from events. Terminal results own
status, errors, output refs, trace ids, conversation-history effects, realtime
task-state effects, and other metadata that is not guaranteed to appear in the
stream.

`AgentGraphRuntime.run_stream()` returns
`AgentRunStream[AgentState]`:

```python
stream = runtime.run_stream(request, cancel_token=cancel_token)
async for event in stream:
    consume(event)
state = await stream.result()
```

`run_assistant_request_stream()` returns
`AgentRunStream[AssistantRunArtifacts]` and preserves the shared service as the
owner of provider/config resolution, runtime construction, conversation
history, realtime task state, context preparation, trace, and final artifacts.
`AgentRunStream.wait()` is an alias for `result()`.

The optional compatibility `EventSink` is forwarded deterministically while
the same events are yielded. At service level, streamed events are also present
in `AssistantRunArtifacts.events`. A worker exception is re-raised after
already-enqueued events drain and is also re-raised by `result()`.

## Thread Model And Ordering

The core runtime and shared assistant service remain synchronous sources of
truth. Their async stream facades are deliberately narrow:

```text
async consumer
  -> run_stream() / run_assistant_request_stream()
  -> asyncio.to_thread(sync runtime or service)
  -> EventSink.emit(AgentEvent) in worker thread
  -> AsyncQueueEventSink
  -> AgentRunStream in owning event loop
```

`asyncio.Queue` is not thread-safe. Worker threads must never call its methods
directly. `AgentRunStream.emit()` schedules queue insertion with
`loop.call_soon_threadsafe()`, and terminal result/exception publication uses
the same loop scheduling boundary. This preserves the order of prior event
callbacks before the terminal sentinel.

The realtime backend normally consumes the shared service stream with
`async for`. Its injected synchronous `run_request=` hook is retained only as a
compatibility wrapper and uses the same worker-thread stream bridge. New
production integrations should prefer the stream interface.

Async migration remains selective:

- keep Gateway, WebSocket, realtime delivery, and supported provider streams
  async-native;
- keep sync-only SDKs, governed tools, local memory, filesystem/artifact work,
  and subprocess-backed operations behind sync/thread boundaries unless
  measured concurrency or latency justifies a focused migration;
- do not duplicate business logic merely to remove `asyncio.to_thread()`.

`ProviderStreamingTurnRunner` bridges an async provider stream into the current
synchronous runtime turn. It uses an event loop directly when called from a
normal worker thread and isolates the coroutine in a helper thread if the
caller already owns a running loop. This is a compatibility bridge, not a
second agent runtime.

## Realtime And Gateway Mapping

`AgentGraphRealtimeBackend` consumes the shared assistant stream, maps each
`AgentEvent` through `assistant_agent.realtime.event_mapping`, and awaits the
realtime event sink. Mapping may produce progress, response chunks, final
response, tool/trace display events, confirmation events, or errors. Final
response chunking and duplicate streamed-delta suppression remain realtime
adapter policy.

Gateway then maps `RealtimeAgentEvent` records to normalized frames, including
`response.chunk -> stream.chunk` and `run.progress -> event.progress`, while
owning run/session lifecycle, reconnect, cancel, interrupt, stale-output
suppression, and transport behavior. Changes to those wire semantics belong in
`docs/gateway-architecture.md`, not here.

Qwen realtime vision 的 Provider delta 与用户可见 Agent stream 是两条独立流。后台
`QwenRealtimeVisionAdapter` 在 persistent WebSocket 内累积 `response.text.delta`，直到
收到 completed `response.done` 后才发布一个结构化 `VideoUnderstandingResult`；这些 delta
不会映射为 `LLMEvent`、`AgentEvent(response_delta)`、`RealtimeAgentEvent(response.chunk)`
或 Gateway `stream.chunk`。最终 Agent stream 仍只来自前台 chat Provider，经现有
commit barrier 与 `AgentRunStream` 进入 realtime/Gateway。视觉 Provider 的首 delta 与总耗时
只作为 prompt-safe scalar diagnostics，不携带 Qwen 原文或 raw event。

## Cancellation And Failure Semantics

Cancellation is cooperative:

```text
Gateway/event-like cancel token
  -> run_assistant_request_stream(..., cancel_token=...)
  -> AgentGraphRuntime.run_state(..., cancel_token=...)
  -> raise_if_cancelled()
  -> runtime nodes and ToolExecutor checks
```

The runtime recognizes tokens with `is_cancelled()`, event-like `is_set()`, or
a boolean `cancelled` attribute. Governed retry backoff checks cancellation in
short intervals. A cancellation handled by the runtime yields its existing
`task_cancelled` event and cancelled terminal state; the stream facade does not
invent a second cancellation protocol.

There is no safe force-kill guarantee for arbitrary blocking work:

- `asyncio.to_thread()` cannot terminate a worker thread;
- a blocking SDK, tool, subprocess, or filesystem call may continue until it
  returns, reaches its timeout, or performs a cooperative check;
- closing/cancelling a provider stream is adapter-specific and must preserve
  provider error and resource-cleanup behavior.

Timeouts, bounded calls, adapter cleanup, structured errors, and cooperative
checks are the supported controls. Do not claim hard preemption without a
separate process or an upstream API that actually provides it.

## Source Ownership

| source | responsibility |
| --- | --- |
| `src/assistant_agent/schemas/llm_events.py` | `LLMEvent`, provider error/tool delta schemas, accumulator |
| `src/assistant_agent/services/chat_adapter.py` | sync chat compatibility, async provider adapters, vendor chunk normalization and cleanup |
| `src/assistant_agent/agent/provider_streaming.py` | runtime-local async provider stream consumption into `ChatResult` |
| `src/assistant_agent/agent/llm_event_mapping.py` | visible token delta to `AgentEvent(response_delta)` mapping |
| `src/assistant_agent/schemas/events.py` | runtime `AgentEvent` contract |
| `src/assistant_agent/agent/event_stream.py` | `AgentRunStream` and thread-safe queue sink |
| `src/assistant_agent/agent/runtime.py` | graph lifecycle, provider-path selection, `run_state`/`run`/`run_stream` |
| `src/assistant_agent/services/assistant_run_service.py` | shared sync and streaming run service, `AssistantRunArtifacts` |
| `src/assistant_agent/realtime/agent_graph_backend.py` | assistant stream consumption and realtime terminal result |
| `src/assistant_agent/realtime/event_mapping.py` | `AgentEvent` to `RealtimeAgentEvent` mapping |
| `src/assistant_agent/gateway/event_mapping.py` | realtime event to Gateway frame mapping |

Adjacent authorities remain authoritative for their domains:

- `docs/gateway-architecture.md`: Gateway frames and lifecycle.
- `docs/tool-calling-architecture.md`: tool validation/execution governance.
- `docs/observability-harness.md`: trace events, persistence, and redaction.
- `docs/CONTEXT_ENGINEERING_STATUS.md`: prompt/context assembly and budgets.

## Update Rules

Update this document in the same change when any of the following changes:

- an `LLMEvent`, `AgentEvent`, or `AgentRunStream` contract;
- provider stream selection, accumulation, callback compatibility, or cleanup;
- stream/result ownership or terminal exception behavior;
- worker-thread/event-loop ordering or queue bridging;
- cancellation guarantees or blocking-call limitations;
- source ownership or realtime mapping before the Gateway frame boundary.

Update the Gateway authority instead when frame names, session/run lifecycle,
cancel/interrupt delivery, reconnect, or WebSocket behavior changes. Historical
files under `docs/superpowers/specs/` and `docs/superpowers/plans/` are
development records, not current architecture authority.

## Offline Validation

Run the minimal risk-driven safety net:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

It protects runtime completion, provider timeout termination, cancellation, and the core event-to-Gateway
conversion contract. Broader realtime behavior is validated through the explicit offline simulators in
`scripts/README.md`; real provider streaming requires a `provider_smoke` or `pilot` profile and local
untracked credentials.
