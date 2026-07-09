# Provider Event Boundary Architecture

Last updated: 2026-07-09

This document defines Phase 6 of the runtime event-stream migration: the LLM
provider event boundary. The goal is to move provider streaming away from
ad-hoc callbacks toward provider-neutral events without changing Gateway frames,
tool governance, memory policy, or the current `ChatAdapter.chat()` result
contract.

## Current State

The chat provider boundary is currently synchronous:

```text
Runtime / assistant loop
  -> ChatAdapter.chat(ChatRequest)
  -> ChatResult
```

Streaming is delivered through `ChatRequest.stream_callback`:

```text
provider chunk
  -> _parse_openai_chat_stream()
  -> _emit_stream_delta(text, payload)
  -> ChatRequest.stream_callback(text, payload)
  -> AgentEvent(type="response_delta")
```

The callback path works, but it is too narrow:

- text deltas are represented as `(text, payload)` pairs instead of structured
  provider events;
- tool-call deltas are parsed inside `chat_adapter.py` and only become visible
  after they have been accumulated into final `ChatResult.tool_calls`;
- Runtime code maps callback payloads directly into `AgentEvent` records;
- future async provider streaming has no internal event contract to target.

## Design Choice

There are three possible paths:

| Option | Description | Trade-off |
| --- | --- | --- |
| Keep callbacks only | Leave `stream_callback(text, payload)` as the only streaming interface. | Lowest risk now, but keeps provider streaming stringly typed and makes async-native provider work harder later. |
| Replace chat adapters with async streams now | Change providers to return async iterators directly. | Architecturally clean, but too much blast radius for the current migration stage. |
| Add a provider-neutral `LLMEvent` boundary first | Parse vendor chunks into internal events, then preserve current callback/result behavior on top. | Recommended. It gives the runtime a better producer contract while keeping existing tests and consumers stable. |

Phase 6 chooses the third option.

## LLMEvent Contract

`LLMEvent` is an internal provider-boundary event, not a Gateway event and not a
replacement for `AgentEvent`.

When implemented, the contract should live near the existing schemas, for
example `src/assistant_agent/schemas/llm_events.py`.

Minimum V1 shape:

```python
LLMEventType = Literal[
    "token_delta",
    "tool_call_delta",
    "completed",
    "error",
]


class LLMToolCallDelta(BaseModel):
    index: int = Field(ge=0)
    id: str | None = None
    type: str | None = "function"
    name_delta: str | None = None
    arguments_delta: str | None = None


class LLMProviderError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class LLMEvent(BaseModel):
    event_type: LLMEventType
    provider: str = Field(min_length=1)
    model: str | None = None
    text: str | None = None
    tool_call_delta: LLMToolCallDelta | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: LLMProviderError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Boundary rules:

- Do not include `session_id`, `run_id`, Gateway frame fields, TTS fields, or UI
  display concerns.
- Do not expose raw OpenAI, Anthropic, Qwen, DeepSeek, Codex, or other vendor
  chunks to Runtime consumers.
- Keep provider and model metadata because they are useful for trace, payload,
  and debugging.
- Keep provider errors in a provider-neutral `LLMProviderError` shape instead
  of importing service-layer chat adapter models into schema code.
- Keep raw provider responses out of `metadata`; only prompt-safe and
  trace-safe values belong there.

## Mapping To AgentEvent

Runtime remains responsible for turning provider events into runtime events.

Recommended V1 mapping:

| LLMEvent | Runtime behavior |
| --- | --- |
| `token_delta` | Map to existing `AgentEvent(type="response_delta")` when the active runtime path is allowed to show text. Preserve current payload keys such as `provider`, `model`, `token_streaming`, and `chunking_strategy`. Runtime owns the `source` value. |
| `tool_call_delta` | Do not emit user-visible `AgentEvent` by default. Accumulate into final native tool calls and optionally record trace-safe debug metadata later. This preserves current preamble buffering and tool-governance behavior. |
| `completed` | Finish the provider stream and contribute `finish_reason`, `usage`, and accumulated content/tool calls to `ChatResult`. |
| `error` | Convert through existing provider error handling into `ChatResult.errors`; Runtime decides whether that becomes `task_failed`, fallback text, or recovery. |

The first implementation should keep `ChatRequest.stream_callback` working by
adapting `LLMEvent(token_delta)` back into the legacy `(text, payload)` callback.
That lets current Runtime and assistant-loop code continue to consume
`response_delta` events while the provider parser becomes event-native inside.

## ChatResult Compatibility

`ChatResult` stays the terminal provider result. Event streaming does not
replace the result.

```text
LLMEvent stream
  -> accumulated content / tool-call deltas / usage / finish reason
  -> ChatResult
```

This mirrors the runtime-level separation between event stream and final result:
events describe progress; result describes terminal outcome.

## Migration Sequence

### Phase 6A: Schema And Accumulator

Add `LLMEvent`, `LLMToolCallDelta`, and a small accumulator helper that can:

- collect token deltas into final response text;
- collect tool-call deltas by index;
- produce finalized `NativeToolCall` payloads;
- allow parser callers to preserve the source `provider_format`, for example
  `openai_compatible`, when finalizing tool calls;
- carry `finish_reason` and `usage`.

Keep this helper independent from any vendor SDK.

### Phase 6B: OpenAI-Compatible Stream Parser

Refactor `_parse_openai_chat_stream()` so vendor chunks first become
`LLMEvent` records. Then build the existing `ChatResult` from the accumulator.

Implemented status: `_parse_openai_chat_stream()` now consumes
`_openai_chat_stream_events()`, feeds `LLMEvent` records into
`LLMEventAccumulator`, and adapts `token_delta` events back into the legacy
`ChatRequest.stream_callback` payload.

Compatibility requirements:

- `test_stream_chunks_aggregate_content` should still see the same legacy
  stream callback text and payload.
- `test_stream_chunks_aggregate_tool_call_arguments` should still produce the
  same final `ChatResult.tool_calls`.
- No public provider config or adapter constructor should change.

### Phase 6C: Runtime Mapping Helper

Add a small mapper from `LLMEvent(token_delta)` to `AgentEvent(response_delta)`.
Use it behind existing runtime callbacks so `assistant_loop_nodes.py`,
`graph_nodes.py`, and `runtime.py` do not each hand-build subtly different
response-delta payloads forever.

Implemented status: `llm_event_mapping.py` now maps provider-neutral
`LLMEvent(token_delta)` records to existing `response_delta` runtime events.
Legacy `(text, payload)` callbacks are adapted through that helper in
`assistant_loop_nodes.py`, `graph_nodes.py`, and `runtime.py`. Runtime-owned
`source` values remain explicit, and callbacks without provider/model metadata
preserve their previous payload shape.

This mapper should not emit user-visible events for `tool_call_delta` in V1.

### Phase 6D: Optional Async Provider Stream

Only after the parser is event-native, add an optional async stream path for
providers that support async clients:

```python
def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
    ...
```

This must be additive. `ChatAdapter.chat()` remains the compatibility contract
until the runtime is ready to consume provider events natively.

Design review status: Phase 6D should add only an optional provider-boundary
async stream contract first; it must not make Runtime, Gateway, TTS, or UI
consume `LLMEvent` directly. See
`docs/superpowers/specs/2026-07-09-provider-async-stream-design.md`.

## Non-Goals

- Do not change Gateway wire frame names.
- Do not make Gateway consume `LLMEvent`.
- Do not replace `AgentEvent`.
- Do not remove `ChatRequest.stream_callback` in Phase 6.
- Do not make all provider adapters async.
- Do not stream raw tool-call arguments to UI or TTS.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not change memory read/write policy.

## Acceptance Criteria

Phase 6 design is complete when the repository records:

- the provider-neutral `LLMEvent` shape;
- the mapping rules from `LLMEvent` to current `AgentEvent` behavior;
- the compatibility rule that `ChatResult` remains the terminal provider result;
- the staged path for refactoring provider streaming before adding async-native
  provider streams.

The next code phase should start with schema and parser tests, not with a broad
runtime rewrite.
