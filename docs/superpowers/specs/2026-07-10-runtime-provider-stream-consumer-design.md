# Runtime Provider Stream Consumer Design

Date: 2026-07-10

## Goal

Add a runtime-internal path that can consume
`AsyncStreamingChatAdapter.stream_chat()` and continue emitting existing
`AgentEvent` records. `LLMEvent` remains a provider-boundary event. Gateway,
Realtime, TTS, UI, public API contracts, memory, and tool execution boundaries
do not consume `LLMEvent` directly.

## Decision

Introduce a small runtime turn controller that converts one provider stream
turn into the same terminal `ChatResult` shape the current native loop already
understands: visible text, accumulated native tool calls, finish metadata,
usage, and provider errors.

The controller is not a new agent loop. It only replaces the per-turn provider
call inside `AgentGraphRuntime._run_native_runtime()` when all of these are
true:

- the current chat adapter exposes `stream_chat(request)`;
- runtime config explicitly enables the native provider stream path;
- the caller is already in the provider-native runtime path.

If any condition is false, Runtime continues to call `ChatAdapter.chat()` and
uses the existing `ChatResult` path.

## Architecture

Current provider-native turn:

```text
AgentGraphRuntime._run_native_runtime()
  -> ChatAdapter.chat(ChatRequest)
  -> ChatResult
  -> content/refusal final answer OR NativeToolCall[]
```

Target optional provider stream turn:

```text
AgentGraphRuntime._run_native_runtime()
  -> ProviderStreamingTurnRunner.run_turn(ChatRequest)
  -> AsyncStreamingChatAdapter.stream_chat()
  -> LLMEvent
  -> AgentEvent(response_delta), only for allowed visible token text
  -> LLMEventAccumulator
  -> ChatResult
  -> existing native loop content/tool/error handling
```

`ProviderStreamingTurnRunner` lives under `assistant_agent.agent` because it is
runtime policy, not provider parsing and not Gateway behavior. Provider adapters
still own vendor chunk normalization into `LLMEvent`.

## Proposed File Boundaries

`src/assistant_agent/agent/provider_streaming.py`

- Defines `ProviderStreamingTurnRunner`.
- Owns the runtime consumption of `LLMEvent` for a single provider turn.
- Emits user-visible `AgentEvent(response_delta)` only for safe
  `token_delta` text.
- Accumulates `tool_call_delta` into `NativeToolCall` with
  `LLMEventAccumulator`.
- Converts terminal `LLMEvent(error)` into `ChatProviderError`.
- Returns a `ChatResult` so `AgentGraphRuntime._run_native_runtime()` can reuse
  its existing success, tool-call, and provider-failure handling.
- Does not execute tools, build context, touch memory, or know Gateway frames.

`src/assistant_agent/agent/runtime.py`

- Keeps the existing native loop as the orchestration owner.
- Calls the runner only when stream support and the runtime flag are present.
- Keeps `ChatAdapter.chat()` fallback unchanged.
- Continues to run native tool calls through
  `ActionValidator -> ToolExecutor -> ToolRegistry`.

`src/assistant_agent/config.py`

- Adds an explicit opt-in config field, for example
  `native_provider_streaming: bool = False`.
- Reads an env var such as
  `MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING=1`.
- Defaults to disabled for mock/local/offline safety.

`tests/test_runtime_provider_streaming.py`

- Covers the new single-turn runner and the runtime integration.
- Uses scripted fake adapters only; no real provider calls.

## Event Mapping Rules

### token_delta

`LLMEvent(token_delta)` may become `AgentEvent(response_delta)` only when:

- `event.text` is non-empty;
- the current provider turn is allowed to show text;
- the stream has not yet produced a tool-call delta for the same turn.

The mapper should reuse `llm_event_to_agent_event(...)` so payload shape stays
consistent with existing `response_delta` events. Runtime owns the `source`
value, for example `native_provider_stream`.

If a provider later returns tool calls in the same turn, first-turn text is
treated as native tool-call preamble. The existing native loop behavior must be
preserved: discard the buffered response deltas, record the preamble in
`native_tool_call_preambles`, and emit the deterministic replaceable
`progress_message` for the selected tool.

### tool_call_delta

`LLMEvent(tool_call_delta)` must never become user-visible
`AgentEvent(response_delta)`. It is accumulated into `NativeToolCall` objects.
Those tool calls return to the existing native loop and are processed as if
they came from `ChatResult.tool_calls`.

The execution boundary remains:

```text
NativeToolCall
  -> native_tool_call_to_assistant_decision()
  -> ActionValidator.validate()
  -> ToolExecutor.run_tool()
  -> ToolRegistry
```

### completed

`LLMEvent(completed)` ends only the current provider turn. It does not mean the
agent run is complete.

If accumulated tool calls exist, Runtime enters the existing native tool loop.
If no tool calls exist, Runtime treats the accumulated text or refusal metadata
as the final provider answer for that turn.

### error

`LLMEvent(error)` is a provider failure terminal event. The runner converts it
to a `ChatProviderError`-compatible result. `AgentGraphRuntime` then uses its
existing `_set_native_runtime_response(...)` failure behavior so terminal
runtime events remain `task_failed` and existing observability paths remain in
charge.

### cancellation

`asyncio.CancelledError`, Gateway interrupt, hangup, and explicit run cancel
are runtime control signals. They must not be converted to `LLMEvent(error)` or
provider failure metadata. Cancellation should propagate to the native runtime
caller and use existing `AgentRunCancelled` / `task_cancelled` behavior.

The provider stream adapter remains responsible for closing its provider stream
when iteration is cancelled or abandoned.

## Fallback Strategy

Runtime should detect stream support structurally:

```python
stream_chat = getattr(chat_adapter, "stream_chat", None)
streaming_supported = callable(stream_chat)
```

Runtime should enable the stream path only when both `streaming_supported` and
`config.native_provider_streaming` are true.

If the stream path is disabled, unsupported, or not in the native provider
runtime, the current `chat_adapter.chat(chat_request)` behavior remains the
source of truth.

## Non-Goals

- Do not make Gateway consume `LLMEvent`.
- Do not make Realtime, TTS, UI, or public API consumers consume `LLMEvent`.
- Do not change Gateway frame names.
- Do not remove `ChatAdapter.chat()` or `ChatRequest.stream_callback`.
- Do not make all providers async.
- Do not execute tools from the stream runner.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not convert cancellation into provider error.
- Do not expose raw provider chunks, raw SDK objects, headers, prompts,
  messages, request bodies, credentials, or provider raw payloads in runtime
  events or trace output.

## Acceptance Criteria

- With streaming disabled, current native runtime tests pass unchanged.
- With streaming enabled and a fake async streaming adapter, direct final text
  produces `AgentEvent(response_delta)` and final response behavior equivalent
  to the sync `ChatResult` path.
- With streaming enabled and tool-call deltas, tool-call arguments are not
  emitted as user-visible response text.
- `finish_reason="tool_calls"` ends only the provider turn and proceeds to the
  existing tool loop.
- Provider stream errors become the existing native runtime failure behavior.
- Cancellation propagates through existing runtime cancellation behavior.
- Gateway, realtime mapping, TTS edge helpers, memory service, and tool
  execution contracts are unchanged.
