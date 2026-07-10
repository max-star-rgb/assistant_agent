# Provider Async Stream Implementation Plan

> **For agentic workers:** Implementers may use superpowers:subagent-driven-development or superpowers:executing-plans to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional provider-boundary `stream_chat()` API that yields `LLMEvent` records without changing `ChatAdapter.chat()`, Runtime, Realtime, or Gateway behavior.

**Architecture:** Keep `ChatAdapter.chat()` and `ChatResult` as the existing compatibility contract. Add `AsyncStreamingChatAdapter` as an optional protocol, implement deterministic mock async streaming, and add an OpenAI-compatible async provider stream that reuses the existing `LLMEvent` parser/accumulator semantics. Runtime and Gateway continue to consume `AgentEvent`, not `LLMEvent`.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, Pydantic, OpenAI SDK 2.44.0 with `AsyncOpenAI`, existing `ChatAdapter`, `ChatRequest`, `ChatResult`, `LLMEvent`, and `LLMEventAccumulator`.

## Global Constraints

- Do not change Gateway wire frame names.
- Do not make Runtime, Gateway, TTS, UI, or public API consumers read `LLMEvent`.
- Do not replace or remove `ChatAdapter.chat()`.
- Do not remove `ChatRequest.stream_callback`.
- Do not replace `ChatResult` as the terminal result for existing runtime paths.
- Do not make all providers async.
- Do not expose raw provider chunks, raw SDK objects, headers, prompts, messages, request bodies, response payloads, API keys, or credentials in `LLMEvent`.
- Do not stream tool-call argument deltas to user-visible response text.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not change Memory, Runtime, Realtime backend, Gateway, or session-history behavior.
- Keep tests mock/local/offline by default.
- Do not commit documentation or planning changes unless the user explicitly asks.

---

## File Structure

- `src/assistant_agent/services/chat_adapter.py`
  - Add optional `AsyncStreamingChatAdapter` protocol.
  - Add `MockChatAdapter.stream_chat()`.
  - Add lazy `AsyncOpenAI` support to `OpenAICompatibleChatAdapter`.
  - Add async OpenAI-compatible stream event translation while preserving current sync `chat()` behavior.
  - Add prompt-safe `LLMProviderError` conversion from existing chat-provider error mapping.
- `tests/test_async_chat_stream.py`
  - New focused async provider stream contract tests.
- `tests/test_direct_chat_adapter.py`
  - Keep existing sync parser and `stream_callback` regression tests; extend only if needed for parity.
- `docs/superpowers/specs/2026-07-09-provider-async-stream-design.md`
  - Reference only if implementation uncovers a design correction. Do not rewrite during implementation without a concrete contract issue.

---

### Task 1: Optional Protocol And Mock Async Stream

**Files:**
- Modify: `src/assistant_agent/services/chat_adapter.py`
- Create: `tests/test_async_chat_stream.py`

**Interfaces:**
- Consumes: `ChatRequest`, `LLMEvent`
- Produces:
  - `class AsyncStreamingChatAdapter(Protocol)`
  - `MockChatAdapter.stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]`

- [ ] **Step 1: Write failing tests for protocol call shape and mock stream**

Create `tests/test_async_chat_stream.py` with:

```python
import inspect
from collections.abc import AsyncIterator

import pytest

from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator
from assistant_agent.services.chat_adapter import (
    AsyncStreamingChatAdapter,
    ChatRequest,
    MockChatAdapter,
)


def chat_request(text: str = "解释一下 Agent") -> ChatRequest:
    return ChatRequest(user_id="u1", session_id="s1", user_query=text)


async def collect_events(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_mock_stream_chat_returns_async_iterator_without_awaiting() -> None:
    adapter: AsyncStreamingChatAdapter = MockChatAdapter()

    stream = adapter.stream_chat(chat_request())

    assert inspect.isawaitable(stream) is False
    assert hasattr(stream, "__aiter__")
    events = await collect_events(stream)
    assert [event.event_type for event in events] == ["token_delta", "completed"]


@pytest.mark.asyncio
async def test_mock_stream_chat_emits_token_delta_then_completed() -> None:
    events = await collect_events(MockChatAdapter().stream_chat(chat_request("你好")))

    token, completed = events
    assert token.event_type == "token_delta"
    assert token.provider == "mock"
    assert token.model == "mock-direct-chat"
    assert token.text is not None
    assert "你好" in token.text
    assert token.metadata == {
        "token_streaming": False,
        "chunking_strategy": "mock_full_text",
    }
    assert completed.event_type == "completed"
    assert completed.provider == "mock"
    assert completed.model == "mock-direct-chat"
    assert completed.finish_reason == "stop"
    assert completed.usage["input_chars"] == len("你好")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py -q
```

Expected: fail because `AsyncStreamingChatAdapter` and `MockChatAdapter.stream_chat()` do not exist.

- [ ] **Step 3: Implement optional protocol and mock stream**

In `src/assistant_agent/services/chat_adapter.py`, update imports:

```python
from collections.abc import AsyncIterator, Callable, Iterator
```

Add the protocol directly after `ChatAdapter`:

```python
class AsyncStreamingChatAdapter(Protocol):
    """Optional provider boundary for async LLM event streams."""

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        """Return provider-neutral streaming events without replacing chat()."""
```

Extract mock response helpers near `MockChatAdapter`:

```python
def _mock_chat_response_text(request: ChatRequest) -> str:
    context_note = ""
    if request.memory_context:
        context_note = f" 已参考 {len(request.memory_context)} 条记忆。"
    return f"已收到你的请求：{request.user_query}。这是一个离线 mock direct_chat 回复。{context_note}".strip()


def _mock_chat_usage(request: ChatRequest) -> dict[str, int]:
    return {
        "input_chars": len(request.user_query),
        "output_chars": 35 + len(request.user_query),
    }
```

Change `MockChatAdapter.chat()` to use the helpers:

```python
response_text = _mock_chat_response_text(request)
usage = _mock_chat_usage(request)
```

and return `usage=usage`.

Add `MockChatAdapter.stream_chat()`:

```python
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        response_text = _mock_chat_response_text(request)
        usage = _mock_chat_usage(request)
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text=response_text,
            metadata={
                "token_streaming": False,
                "chunking_strategy": "mock_full_text",
            },
        )
        yield LLMEvent(
            event_type="completed",
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            usage=usage,
        )
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py tests/test_direct_chat_adapter.py::test_mock_chat_adapter_returns_structured_result -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Review checkpoint, no commit**

Run:

```bash
git diff -- src/assistant_agent/services/chat_adapter.py tests/test_async_chat_stream.py
```

Expected: only optional protocol, mock helper extraction, mock async stream, and tests changed. Do not commit unless the user explicitly asks.

---

### Task 2: Shared OpenAI-Compatible Chunk Translation For Sync And Async

**Files:**
- Modify: `src/assistant_agent/services/chat_adapter.py`
- Modify: `tests/test_direct_chat_adapter.py`
- Modify: `tests/test_async_chat_stream.py`

**Interfaces:**
- Consumes: existing `_openai_chat_stream_events(stream, provider, model) -> Iterator[LLMEvent]`
- Produces:
  - reusable `_OpenAIStreamState`
  - reusable `_openai_chat_chunk_events(...)`
  - `_openai_stream_completed_event(...)`
  - unchanged sync `_openai_chat_stream_events(...)`

- [ ] **Step 1: Add parser parity tests before refactor**

Extend `tests/test_async_chat_stream.py` with helper fixtures that will be used by async tests later:

```python
class FakeAsyncStream:
    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self) -> "FakeAsyncStream":
        return self

    async def __anext__(self) -> dict:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed = True
```

Then add a sync helper parity test to `tests/test_direct_chat_adapter.py` if not already covered:

```python
def test_openai_stream_ignores_empty_keepalive_chunks() -> None:
    events = list(
        chat_adapter_module._openai_chat_stream_events(
            [
                {"model": "deepseek-chat", "choices": []},
                {"choices": [{"delta": {}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ],
            provider="deepseek",
            model="fallback",
        )
    )

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    assert events[0].text == "ok"
    assert events[-1].finish_reason == "stop"
```

- [ ] **Step 2: Run focused parser tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_direct_chat_adapter.py::test_openai_stream_chunks_are_converted_to_llm_events tests/test_direct_chat_adapter.py::test_openai_stream_tool_call_chunks_are_converted_to_llm_events tests/test_direct_chat_adapter.py::test_openai_stream_ignores_empty_keepalive_chunks -q
```

Expected: existing tests pass; new test should pass before refactor if behavior already matches.

- [ ] **Step 3: Refactor OpenAI stream chunk translation without changing behavior**

In `src/assistant_agent/services/chat_adapter.py`, add this dataclass near stream helpers:

```python
from dataclasses import dataclass, field


@dataclass
class _OpenAIStreamState:
    response_model: str
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    refusal_parts: list[str] = field(default_factory=list)
```

Extract the body of `_openai_chat_stream_events()` into:

```python
def _openai_chat_chunk_events(
    chunk: Any,
    *,
    provider: str,
    state: _OpenAIStreamState,
) -> Iterator[LLMEvent]:
    data = _to_plain_data(chunk)
    if not isinstance(data, dict):
        return
    if data.get("model"):
        state.response_model = str(data["model"])
    chunk_usage = data.get("usage")
    if isinstance(chunk_usage, dict):
        state.usage = chunk_usage
    choices = data.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason") is not None:
            state.finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            yield LLMEvent(
                event_type="token_delta",
                provider=provider,
                model=state.response_model,
                text=content,
                finish_reason=state.finish_reason,
                metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
            )
        elif isinstance(content, list):
            chunk_text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            if chunk_text:
                yield LLMEvent(
                    event_type="token_delta",
                    provider=provider,
                    model=state.response_model,
                    text=chunk_text,
                    finish_reason=state.finish_reason,
                    metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
                )
        refusal = delta.get("refusal")
        if isinstance(refusal, str):
            state.refusal_parts.append(refusal)
        yield from _openai_tool_call_delta_events(
            delta.get("tool_calls"),
            provider=provider,
            model=state.response_model,
        )
```

Add:

```python
def _openai_stream_completed_event(*, provider: str, state: _OpenAIStreamState) -> LLMEvent:
    metadata: dict[str, Any] = {}
    refusal_text = "".join(state.refusal_parts or [])
    if refusal_text:
        metadata["refusal"] = refusal_text
    return LLMEvent(
        event_type="completed",
        provider=provider,
        model=state.response_model,
        finish_reason=state.finish_reason,
        usage=dict(state.usage or {}),
        metadata=metadata,
    )
```

Rewrite `_openai_chat_stream_events()` to:

```python
def _openai_chat_stream_events(stream: Any, *, provider: str, model: str) -> Iterator[LLMEvent]:
    """Translate OpenAI-compatible stream chunks into provider-neutral events."""

    state = _OpenAIStreamState(response_model=model)
    for chunk in stream:
        yield from _openai_chat_chunk_events(chunk, provider=provider, state=state)
    yield _openai_stream_completed_event(provider=provider, state=state)
```

- [ ] **Step 4: Run parser parity tests again**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_direct_chat_adapter.py::test_openai_stream_chunks_are_converted_to_llm_events tests/test_direct_chat_adapter.py::test_openai_stream_tool_call_chunks_are_converted_to_llm_events tests/test_stream_chunks_aggregate_content tests/test_stream_chunks_aggregate_tool_call_arguments -q
```

Expected: all selected tests pass; sync `chat()` behavior is unchanged.

- [ ] **Step 5: Review checkpoint, no commit**

Run:

```bash
git diff -- src/assistant_agent/services/chat_adapter.py tests/test_direct_chat_adapter.py tests/test_async_chat_stream.py
```

Expected: stream parsing is factored for reuse, but sync parser output and callback tests are unchanged. Do not commit unless the user explicitly asks.

---

### Task 3: OpenAI-Compatible Async Stream And Terminal Error Semantics

**Files:**
- Modify: `src/assistant_agent/services/chat_adapter.py`
- Modify: `tests/test_async_chat_stream.py`

**Interfaces:**
- Consumes:
  - `AsyncOpenAI`
  - `_build_chat_completions_payload(...)`
  - `_openai_chat_chunk_events(...)`
  - `_openai_stream_completed_event(...)`
  - `_chat_error_from_exception(...)`
- Produces:
  - `OpenAICompatibleChatAdapter.stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]`
  - `_openai_async_chat_stream_events(...) -> AsyncIterator[LLMEvent]`
  - `_llm_error_event_from_exception(...) -> LLMEvent`
  - `_close_provider_stream(...)`

- [ ] **Step 1: Write failing async OpenAI-compatible tests**

Extend `tests/test_async_chat_stream.py`:

```python
from assistant_agent.services.chat_adapter import OpenAICompatibleChatAdapter


class FakeAsyncCompletions:
    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAsyncChat:
    def __init__(self, completions: FakeAsyncCompletions) -> None:
        self.completions = completions


class FakeAsyncSDKClient:
    def __init__(self, completions: FakeAsyncCompletions) -> None:
        self.chat = FakeAsyncChat(completions)


def async_adapter(*, response=None, error: Exception | None = None):
    completions = FakeAsyncCompletions(response=response, error=error)
    adapter = OpenAICompatibleChatAdapter(
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        async_client=FakeAsyncSDKClient(completions),
    )
    return adapter, completions


@pytest.mark.asyncio
async def test_openai_async_stream_maps_text_and_completion() -> None:
    adapter, completions = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [{"delta": {"content": "真实"}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"content": " 回复"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert completions.calls[0]["stream"] is True
    assert [event.event_type for event in events] == ["token_delta", "token_delta", "completed"]
    assert [event.text for event in events[:2]] == ["真实", " 回复"]
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == {"prompt_tokens": 4, "completion_tokens": 2}


@pytest.mark.asyncio
async def test_openai_async_stream_maps_interleaved_tool_call_deltas() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "delta": {
                                "content": "我查一下。",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "product_", "arguments": '{"query": "通勤'},
                                    },
                                    {
                                        "index": 1,
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": '{"query": "新闻"}'},
                                    },
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"name": "search", "arguments": '耳机"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("帮我查")))

    assert [event.event_type for event in events] == [
        "token_delta",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "completed",
    ]
    assert events[0].text == "我查一下。"
    first_tool_delta = events[1].tool_call_delta
    third_tool_delta = events[3].tool_call_delta
    assert first_tool_delta is not None
    assert third_tool_delta is not None
    assert first_tool_delta.name_delta == "product_"
    assert third_tool_delta.name_delta == "search"
    assert events[-1].finish_reason == "tool_calls"
    assert all("raw_secret" not in repr(event) for event in events)

    accumulator = LLMEventAccumulator()
    for event in events:
        accumulator.apply(event)
    calls = accumulator.finalize_tool_calls(provider_format="openai_compatible")
    assert [call.name for call in calls] == ["product_search", "web_search"]
    assert calls[0].arguments == {"query": "通勤耳机"}
    assert calls[1].arguments == {"query": "新闻"}


@pytest.mark.asyncio
async def test_openai_async_stream_provider_error_is_terminal() -> None:
    adapter, _ = async_adapter(error=TimeoutError("provider timed out"))

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert [event.event_type for event in events] == ["error"]
    assert events[0].error is not None
    assert events[0].error.code == "provider_timeout"
    assert events[0].error.recoverable is True
```

- [ ] **Step 2: Run async tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py -q
```

Expected: fail because `OpenAICompatibleChatAdapter.__init__()` does not accept `async_client`, and `stream_chat()` is not implemented.

- [ ] **Step 3: Implement async OpenAI-compatible stream support**

Update imports in `src/assistant_agent/services/chat_adapter.py`:

```python
import inspect
from openai import AsyncOpenAI
from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator, LLMProviderError, LLMToolCallDelta
```

Add `async_client` to `OpenAICompatibleChatAdapter.__init__()`:

```python
        async_client: Any | None = None,
```

Store it:

```python
        self._async_client = async_client
```

Add a lazy async SDK accessor:

```python
    def _async_sdk_client(self) -> Any:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )
        return self._async_client
```

Add `OpenAICompatibleChatAdapter.stream_chat()`:

```python
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        payload = _build_chat_completions_payload(
            request,
            self.model,
            self.capabilities,
            stream=True,
        )
        stream: Any | None = None
        try:
            stream_result = self._async_sdk_client().chat.completions.create(**payload)
            stream = await stream_result if inspect.isawaitable(stream_result) else stream_result
            async for event in _openai_async_chat_stream_events(
                stream,
                provider=self.provider,
                model=self.model,
            ):
                yield event
        except (
            ProviderAdapterError,
            APITimeoutError,
            TimeoutError,
            AuthenticationError,
            PermissionDeniedError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            OpenAIError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            yield _llm_error_event_from_exception(self.provider, self.model, exc)
        finally:
            if stream is not None:
                await _close_provider_stream(stream)
```

Add async stream helper:

```python
async def _openai_async_chat_stream_events(stream: Any, *, provider: str, model: str) -> AsyncIterator[LLMEvent]:
    """Translate OpenAI-compatible async stream chunks into provider-neutral events."""

    state = _OpenAIStreamState(response_model=model)
    async for chunk in stream:
        for event in _openai_chat_chunk_events(chunk, provider=provider, state=state):
            yield event
    yield _openai_stream_completed_event(provider=provider, state=state)
```

Add error event conversion:

```python
def _llm_error_event_from_exception(provider: str, model: str | None, exc: Exception) -> LLMEvent:
    result = _chat_error_from_exception(provider, exc)
    error = result.errors[0] if result.errors else ChatProviderError(
        code="provider_unknown_error",
        message="provider error",
        recoverable=False,
    )
    return LLMEvent(
        event_type="error",
        provider=provider,
        model=model,
        error=LLMProviderError(
            code=error.code,
            message=error.message,
            recoverable=error.recoverable,
        ),
    )
```

Add stream cleanup:

```python
async def _close_provider_stream(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        result = aclose()
        if inspect.isawaitable(result):
            await result
        return
    close = getattr(stream, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
```

- [ ] **Step 4: Run async provider tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py -q
```

Expected: all async provider stream tests pass.

- [ ] **Step 5: Run sync regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_direct_chat_adapter.py tests/test_llm_events.py -q
```

Expected: sync `chat()`, `stream_callback`, parser, and accumulator tests still pass.

- [ ] **Step 6: Review checkpoint, no commit**

Run:

```bash
git diff -- src/assistant_agent/services/chat_adapter.py tests/test_async_chat_stream.py tests/test_direct_chat_adapter.py
```

Expected: changes are limited to optional async provider streaming and parser helper reuse. Do not commit unless the user explicitly asks.

---

### Task 4: Cleanup Contract, Terminal Invariants, And Raw Payload Guards

**Files:**
- Modify: `tests/test_async_chat_stream.py`
- Modify: `src/assistant_agent/services/chat_adapter.py` only if tests reveal a gap

**Interfaces:**
- Consumes:
  - `OpenAICompatibleChatAdapter.stream_chat(...)`
  - `_close_provider_stream(...)`
  - `LLMEvent`
- Produces: explicit tests for terminal event invariants, cancellation/cleanup, and raw payload safety.

- [ ] **Step 1: Add cleanup and terminal invariant tests**

Extend `tests/test_async_chat_stream.py`:

```python
@pytest.mark.asyncio
async def test_openai_async_stream_aclose_closes_provider_stream() -> None:
    fake_stream = FakeAsyncStream(
        [
            {
                "model": "deepseek-chat",
                "choices": [{"delta": {"content": "first"}, "finish_reason": None}],
            },
            {
                "choices": [{"delta": {"content": "second"}, "finish_reason": "stop"}],
            },
        ]
    )
    adapter, _ = async_adapter(response=fake_stream)

    stream = adapter.stream_chat(chat_request("hello"))
    first = await anext(stream)
    await stream.aclose()

    assert first.event_type == "token_delta"
    assert fake_stream.closed is True


@pytest.mark.asyncio
async def test_openai_async_stream_emits_no_event_after_completed() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    assert events[-1].event_type == "completed"


@pytest.mark.asyncio
async def test_openai_async_stream_excludes_raw_provider_objects_from_events() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "raw_secret": "must_not_leak",
                    "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                },
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("hello")))
    rendered = "\n".join(repr(event) for event in events)
    payloads = [event.model_dump(mode="json") for event in events]

    assert "must_not_leak" not in rendered
    assert "must_not_leak" not in repr(payloads)
```

- [ ] **Step 2: Run tests to verify behavior**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py -q
```

Expected: all async stream contract tests pass. If a cleanup or raw-payload guard fails, fix only `chat_adapter.py`.

- [ ] **Step 3: Check cancellation is not swallowed**

Add this test if `asyncio.CancelledError` propagation is not already covered by the prior tests:

```python
import asyncio


class CancellingAsyncCompletions(FakeAsyncCompletions):
    async def create(self, **payload):
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_openai_async_stream_does_not_convert_cancellation_to_error() -> None:
    completions = CancellingAsyncCompletions(response=None)
    adapter = OpenAICompatibleChatAdapter(
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        async_client=FakeAsyncSDKClient(completions),
    )

    with pytest.raises(asyncio.CancelledError):
        await collect_events(adapter.stream_chat(chat_request("hello")))
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py -q
```

Expected: all async stream contract tests pass.

- [ ] **Step 5: Review checkpoint, no commit**

Run:

```bash
git diff -- tests/test_async_chat_stream.py src/assistant_agent/services/chat_adapter.py
```

Expected: tests encode terminal invariants, cleanup, and no raw payload leakage. Do not commit unless the user explicitly asks.

---

### Task 5: Full Regression Verification

**Files:**
- No required source changes

**Interfaces:**
- Verifies all prior tasks.

- [ ] **Step 1: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 2: Run provider-focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_async_chat_stream.py tests/test_direct_chat_adapter.py tests/test_llm_events.py -q
```

Expected: all selected provider tests pass.

- [ ] **Step 3: Run runtime event regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_events.py tests/test_realtime_event_mapping.py -q
```

Expected: runtime `AgentEvent` and realtime mapping behavior still pass without updates.

- [ ] **Step 4: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all fast tests pass.

- [ ] **Step 5: Final review checkpoint, no commit**

Run:

```bash
git status --short
git diff --stat
```

Expected:

- source changes are limited to `src/assistant_agent/services/chat_adapter.py`;
- test changes are limited to `tests/test_async_chat_stream.py` and focused parity additions in `tests/test_direct_chat_adapter.py`;
- no Runtime, Gateway, Realtime backend, TTS, UI, Memory, ToolExecutor, or ToolRegistry files changed;
- no git commit is created unless the user explicitly asks.
