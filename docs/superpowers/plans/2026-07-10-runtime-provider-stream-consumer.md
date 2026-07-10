# Runtime Provider Stream Consumer Implementation Plan

> **For agentic workers:** Implementers may use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `AgentGraphRuntime` optionally consume `AsyncStreamingChatAdapter.stream_chat()` internally while preserving `AgentEvent` as the runtime output contract.

**Architecture:** Add a runtime-local provider stream runner that turns `LLMEvent` records into a `ChatResult`, adapting visible token deltas back through the existing `ChatRequest.stream_callback` path. Gate the path behind an explicit config flag and structural adapter support. Keep Gateway, Realtime, TTS, UI, memory, and tool execution contracts unchanged.

**Tech Stack:** Python 3.12, dataclasses, asyncio, pytest, existing `AgentGraphRuntime`, `ChatRequest`, `ChatResult`, `AsyncStreamingChatAdapter`, `LLMEventAccumulator`, `AgentEvent`, and `EventSink`.

## Global Constraints

- Do not make Gateway, Realtime, TTS, UI, or public API consumers read `LLMEvent`.
- Do not remove `ChatAdapter.chat()` or `ChatRequest.stream_callback`.
- Do not make all providers async.
- Do not change Gateway frame names or realtime event type names.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not stream tool-call argument deltas to user-visible response text.
- Do not convert `asyncio.CancelledError`, Gateway interrupt, hangup, or explicit run cancel into provider errors.
- Do not expose raw provider chunks, raw SDK objects, prompts, messages, headers, credentials, or provider raw payloads in runtime events or traces.
- Keep mock/local/offline behavior stable by default.
- Do not create a git commit unless the user explicitly asks.

---

## File Structure

- Create: `src/assistant_agent/agent/provider_streaming.py`
  - Runtime-local single provider-turn runner.
  - Consumes `AsyncStreamingChatAdapter.stream_chat()`.
  - Accumulates `LLMEvent` records into `ChatResult`.
  - Calls `ChatRequest.stream_callback` only for visible token deltas.
  - Does not execute tools or know Gateway frames.
- Modify: `src/assistant_agent/config.py`
  - Add explicit `native_provider_streaming: bool = False`.
  - Parse `MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING` through existing `_bool_env`.
- Modify: `src/assistant_agent/agent/runtime.py`
  - Add a small helper that chooses stream runner or existing `chat_adapter.chat()`.
  - Use it in the normal native turn and final-only handoff turn.
- Create: `tests/test_runtime_provider_streaming.py`
  - New focused runtime provider stream tests.
- Modify: `tests/test_provider_selection.py`
  - Small config env/default tests, if not kept in `tests/test_runtime_provider_streaming.py`.

---

### Task 1: Config Gate For Native Provider Streaming

**Files:**
- Modify: `src/assistant_agent/config.py`
- Test: `tests/test_runtime_provider_streaming.py`

**Interfaces:**
- Consumes: `ProviderConfig.from_env(...)`, `_bool_env(...)`
- Produces: `ProviderConfig.native_provider_streaming: bool`

- [ ] **Step 1: Write failing config tests**

Add to `tests/test_runtime_provider_streaming.py`:

```python
from assistant_agent.config import ProviderConfig


def test_native_provider_streaming_defaults_to_disabled() -> None:
    config = ProviderConfig.from_env({})

    assert config.native_provider_streaming is False


def test_native_provider_streaming_env_flag_enables_runtime_stream_path() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "1"})

    assert config.native_provider_streaming is True
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_native_provider_streaming_defaults_to_disabled tests/test_runtime_provider_streaming.py::test_native_provider_streaming_env_flag_enables_runtime_stream_path -q
```

Expected: fail because `ProviderConfig` has no `native_provider_streaming`.

- [ ] **Step 3: Add config field and env parsing**

In `src/assistant_agent/config.py`, add the dataclass field near `chat_stream`:

```python
    native_provider_streaming: bool = False
```

In `ProviderConfig.from_env(...)`, pass the field near `chat_stream`:

```python
            chat_stream=_chat_stream(source, chat_provider),
            native_provider_streaming=_bool_env(
                source.get("MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING"),
                False,
            ),
```

- [ ] **Step 4: Run config tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_native_provider_streaming_defaults_to_disabled tests/test_runtime_provider_streaming.py::test_native_provider_streaming_env_flag_enables_runtime_stream_path -q
```

Expected: both tests pass.

---

### Task 2: Single-Turn Provider Stream Runner

**Files:**
- Create: `src/assistant_agent/agent/provider_streaming.py`
- Modify: `tests/test_runtime_provider_streaming.py`

**Interfaces:**
- Consumes:
  - `AsyncStreamingChatAdapter.stream_chat(request) -> AsyncIterator[LLMEvent]`
  - `ChatRequest`
  - `ChatResult`
  - `LLMEventAccumulator`
- Produces:
  - `supports_async_streaming_chat(adapter: object) -> bool`
  - `ProviderStreamingTurnRunner.run_turn(adapter: object, request: ChatRequest) -> ChatResult`

- [ ] **Step 1: Write failing runner tests**

Add these helpers and tests to `tests/test_runtime_provider_streaming.py`:

```python
import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from assistant_agent.agent.provider_streaming import (
    ProviderStreamingTurnRunner,
    supports_async_streaming_chat,
)
from assistant_agent.schemas.llm_events import LLMEvent, LLMProviderError, LLMToolCallDelta
from assistant_agent.services.chat_adapter import ChatRequest


class ScriptedStreamingChatAdapter:
    provider = "scripted-stream"
    model = "stream-model"

    def __init__(self, scripts: list[list[LLMEvent]]) -> None:
        self.scripts = list(scripts)
        self.requests: list[ChatRequest] = []

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        events = self.scripts.pop(0)

        async def stream() -> AsyncIterator[LLMEvent]:
            for event in events:
                yield event

        return stream()


class SyncOnlyChatAdapter:
    provider = "sync-only"

    def chat(self, request: ChatRequest) -> Any:
        raise AssertionError("not used in supports_async_streaming_chat test")


def chat_request(callback=None) -> ChatRequest:
    return ChatRequest(
        user_id="u1",
        session_id="s1",
        user_query="hello",
        stream_callback=callback,
    )


def test_supports_async_streaming_chat_detects_optional_protocol() -> None:
    assert supports_async_streaming_chat(ScriptedStreamingChatAdapter([])) is True
    assert supports_async_streaming_chat(SyncOnlyChatAdapter()) is False


def test_provider_stream_runner_returns_chat_result_and_emits_visible_token_delta() -> None:
    callback_events: list[tuple[str, dict[str, Any]]] = []
    adapter = ScriptedStreamingChatAdapter(
        [
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="hello",
                    metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                    usage={"completion_tokens": 1},
                ),
            ]
        ]
    )

    result = ProviderStreamingTurnRunner().run_turn(
        adapter,
        chat_request(lambda text, payload: callback_events.append((text, payload))),
    )

    assert result.success is True
    assert result.response_text == "hello"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.message_kind == "final_answer"
    assert result.provider == "scripted-stream"
    assert result.model == "stream-model"
    assert result.usage == {"completion_tokens": 1}
    assert callback_events == [
        (
            "hello",
            {
                "token_streaming": True,
                "chunking_strategy": "provider_token_delta",
                "provider": "scripted-stream",
                "model": "stream-model",
            },
        )
    ]


def test_provider_stream_runner_accumulates_tool_calls_without_streaming_arguments() -> None:
    callback_events: list[tuple[str, dict[str, Any]]] = []
    adapter = ScriptedStreamingChatAdapter(
        [
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="checking",
                    metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
                ),
                LLMEvent(
                    event_type="tool_call_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    tool_call_delta=LLMToolCallDelta(
                        index=0,
                        id="call_1",
                        type="function",
                        name_delta="product_",
                        arguments_delta='{"query": "commute',
                    ),
                ),
                LLMEvent(
                    event_type="tool_call_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    tool_call_delta=LLMToolCallDelta(
                        index=0,
                        name_delta="search",
                        arguments_delta=' headphones"}',
                    ),
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    result = ProviderStreamingTurnRunner().run_turn(
        adapter,
        chat_request(lambda text, payload: callback_events.append((text, payload))),
    )

    assert result.success is True
    assert result.response_text == "checking"
    assert result.finish_reason == "tool_calls"
    assert result.message_kind == "tool_call"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "product_search"
    assert result.tool_calls[0].arguments == {"query": "commute headphones"}
    assert callback_events == [
        (
            "checking",
            {
                "token_streaming": True,
                "chunking_strategy": "provider_token_delta",
                "provider": "scripted-stream",
                "model": "stream-model",
            },
        )
    ]
    assert "commute headphones" not in repr(callback_events)


def test_provider_stream_runner_converts_terminal_provider_error() -> None:
    adapter = ScriptedStreamingChatAdapter(
        [
            [
                LLMEvent(
                    event_type="error",
                    provider="scripted-stream",
                    model="stream-model",
                    error=LLMProviderError(
                        code="provider_timeout",
                        message="Chat provider request timed out.",
                        recoverable=True,
                    ),
                )
            ]
        ]
    )

    result = ProviderStreamingTurnRunner().run_turn(adapter, chat_request())

    assert result.success is False
    assert result.provider == "scripted-stream"
    assert result.model == "stream-model"
    assert result.errors[0].code == "provider_timeout"
    assert result.errors[0].recoverable is True


class CancellingStreamingChatAdapter(ScriptedStreamingChatAdapter):
    def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        async def stream() -> AsyncIterator[LLMEvent]:
            raise asyncio.CancelledError()
            yield LLMEvent(event_type="completed", provider="scripted-stream")

        return stream()


def test_provider_stream_runner_does_not_convert_cancelled_error_to_provider_error() -> None:
    with pytest.raises(asyncio.CancelledError):
        ProviderStreamingTurnRunner().run_turn(CancellingStreamingChatAdapter([]), chat_request())
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_supports_async_streaming_chat_detects_optional_protocol tests/test_runtime_provider_streaming.py::test_provider_stream_runner_returns_chat_result_and_emits_visible_token_delta tests/test_runtime_provider_streaming.py::test_provider_stream_runner_accumulates_tool_calls_without_streaming_arguments tests/test_runtime_provider_streaming.py::test_provider_stream_runner_converts_terminal_provider_error tests/test_runtime_provider_streaming.py::test_provider_stream_runner_does_not_convert_cancelled_error_to_provider_error -q
```

Expected: fail because `assistant_agent.agent.provider_streaming` does not exist.

- [ ] **Step 3: Implement provider stream runner**

Create `src/assistant_agent/agent/provider_streaming.py`:

```python
"""Runtime-local consumption of provider LLM event streams."""

from __future__ import annotations

import asyncio
import threading
from time import perf_counter
from typing import Any

from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult


def supports_async_streaming_chat(adapter: object) -> bool:
    return callable(getattr(adapter, "stream_chat", None))


class ProviderStreamingTurnRunner:
    """Run one provider stream turn and return the existing ChatResult shape."""

    def run_turn(self, adapter: object, request: ChatRequest) -> ChatResult:
        return _run_coro_sync(self._run_turn_async(adapter, request))

    async def _run_turn_async(self, adapter: object, request: ChatRequest) -> ChatResult:
        started_at = perf_counter()
        accumulator = LLMEventAccumulator()
        provider = str(getattr(adapter, "provider", "unknown") or "unknown")
        model = getattr(adapter, "model", None)
        refusal: str | None = None
        terminal_seen = False

        stream_chat = getattr(adapter, "stream_chat")
        async for event in stream_chat(request):
            provider = event.provider
            if event.model is not None:
                model = event.model
            if event.event_type == "error":
                terminal_seen = True
                return _chat_result_from_error_event(
                    event,
                    provider=provider,
                    model=model,
                    latency_ms=_elapsed_ms(started_at),
                )
            accumulator.apply(event)
            if event.event_type == "token_delta":
                _emit_token_delta_callback(request, event)
            elif event.event_type == "completed":
                terminal_seen = True
                raw_refusal = event.metadata.get("refusal")
                if isinstance(raw_refusal, str) and raw_refusal:
                    refusal = raw_refusal

        if not terminal_seen:
            return ChatResult(
                response_text="",
                provider=provider,
                model=model,
                latency_ms=_elapsed_ms(started_at),
                errors=[
                    ChatProviderError(
                        code="provider_bad_response",
                        message="chat provider stream ended without a terminal event",
                        recoverable=False,
                    )
                ],
            )

        response_text = accumulator.response_text
        tool_calls = accumulator.finalize_tool_calls(provider_format="openai_compatible")
        return ChatResult(
            response_text=response_text,
            tool_calls=tool_calls,
            finish_reason=accumulator.finish_reason,
            refusal=refusal,
            message_kind=_message_kind(tool_calls=tool_calls, refusal=refusal, content=response_text),
            provider=accumulator.provider or provider,
            model=accumulator.model or model,
            usage=accumulator.usage,
            latency_ms=_elapsed_ms(started_at),
            output_ref=f"provider://chat/{accumulator.provider or provider}",
        )


def _emit_token_delta_callback(request: ChatRequest, event: LLMEvent) -> None:
    if request.stream_callback is None or not event.text:
        return
    payload = dict(event.metadata)
    payload.setdefault("token_streaming", True)
    payload.setdefault("chunking_strategy", "provider_token_delta")
    payload["provider"] = event.provider
    if event.model is not None:
        payload["model"] = event.model
    request.stream_callback(event.text, payload)


def _chat_result_from_error_event(
    event: LLMEvent,
    *,
    provider: str,
    model: str | None,
    latency_ms: int,
) -> ChatResult:
    error = event.error
    return ChatResult(
        response_text="",
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        errors=[
            ChatProviderError(
                code=error.code if error is not None else "provider_unknown_error",
                message=error.message if error is not None else "Chat provider error.",
                recoverable=error.recoverable if error is not None else False,
            )
        ],
    )


def _message_kind(*, tool_calls: list[Any], refusal: str | None, content: str) -> str:
    if tool_calls:
        return "tool_call"
    if refusal:
        return "refusal"
    if content.strip():
        return "final_answer"
    return "empty"


def _elapsed_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box["result"]
```

- [ ] **Step 4: Run runner tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_supports_async_streaming_chat_detects_optional_protocol tests/test_runtime_provider_streaming.py::test_provider_stream_runner_returns_chat_result_and_emits_visible_token_delta tests/test_runtime_provider_streaming.py::test_provider_stream_runner_accumulates_tool_calls_without_streaming_arguments tests/test_runtime_provider_streaming.py::test_provider_stream_runner_converts_terminal_provider_error tests/test_runtime_provider_streaming.py::test_provider_stream_runner_does_not_convert_cancelled_error_to_provider_error -q
```

Expected: all selected tests pass.

---

### Task 3: Runtime Integration With Fallback

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `tests/test_runtime_provider_streaming.py`

**Interfaces:**
- Consumes:
  - `ProviderConfig.native_provider_streaming`
  - `ProviderStreamingTurnRunner.run_turn(...)`
  - `supports_async_streaming_chat(...)`
- Produces:
  - `AgentGraphRuntime._run_native_chat_turn(chat_request: ChatRequest) -> ChatResult`

- [ ] **Step 1: Add runtime integration tests**

Append to `tests/test_runtime_provider_streaming.py`:

```python
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatResult
from assistant_agent.services.event_sink import ListEventSink


class StreamingAndSyncChatAdapter(ScriptedStreamingChatAdapter):
    def __init__(self, scripts: list[list[LLMEvent]], sync_result: ChatResult | None = None) -> None:
        super().__init__(scripts)
        self.sync_result = sync_result or ChatResult(
            response_text="sync fallback",
            finish_reason="stop",
            message_kind="final_answer",
            provider="sync-fallback",
            model="sync-model",
        )
        self.sync_calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        self.sync_calls += 1
        self.requests.append(request)
        return self.sync_result


def test_runtime_uses_sync_chat_when_native_provider_streaming_disabled() -> None:
    adapter = StreamingAndSyncChatAdapter(
        scripts=[
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="stream answer",
                ),
                LLMEvent(event_type="completed", provider="scripted-stream", model="stream-model"),
            ]
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=False),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="hello"))

    assert adapter.sync_calls == 1
    assert state.response is not None
    assert state.response.message == "sync fallback"


def test_runtime_streaming_direct_final_answer_emits_agent_response_delta() -> None:
    adapter = StreamingAndSyncChatAdapter(
        scripts=[
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="stream answer",
                    metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                ),
            ]
        ]
    )
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    )

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        event_sink=sink,
    )

    deltas = [event for event in sink.events if event.type == "response_delta"]
    assert adapter.sync_calls == 0
    assert state.status == "completed"
    assert state.response is not None
    assert state.response.message == "stream answer"
    assert [event.text for event in deltas] == ["stream answer"]
    assert deltas[0].payload["provider"] == "scripted-stream"
    assert deltas[0].payload["source"] == "assistant_native_final_answer"
    assert sink.events[-1].type == "final_response"
```

- [ ] **Step 2: Run runtime integration tests and verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_runtime_uses_sync_chat_when_native_provider_streaming_disabled tests/test_runtime_provider_streaming.py::test_runtime_streaming_direct_final_answer_emits_agent_response_delta -q
```

Expected: first test passes or fails on missing config depending on Task 1 state; second fails because Runtime still calls `chat()`.

- [ ] **Step 3: Wire runtime helper**

In `src/assistant_agent/agent/runtime.py`, import:

```python
from assistant_agent.agent.provider_streaming import ProviderStreamingTurnRunner, supports_async_streaming_chat
```

Add a helper method on `AgentGraphRuntime` near `_run_native_runtime(...)`:

```python
    def _run_native_chat_turn(self, chat_request: ChatRequest) -> ChatResult:
        if self.config.native_provider_streaming and supports_async_streaming_chat(self.chat_adapter):
            return ProviderStreamingTurnRunner().run_turn(self.chat_adapter, chat_request)
        return self.chat_adapter.chat(chat_request)
```

Replace both native chat calls:

```python
            result = self.chat_adapter.chat(chat_request)
```

and

```python
        result = self.chat_adapter.chat(chat_request)
```

with:

```python
            result = self._run_native_chat_turn(chat_request)
```

and:

```python
        result = self._run_native_chat_turn(chat_request)
```

- [ ] **Step 4: Run runtime integration tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_runtime_uses_sync_chat_when_native_provider_streaming_disabled tests/test_runtime_provider_streaming.py::test_runtime_streaming_direct_final_answer_emits_agent_response_delta -q
```

Expected: both tests pass.

---

### Task 4: Streaming Tool-Call Turn Preserves Native Tool Loop

**Files:**
- Modify: `tests/test_runtime_provider_streaming.py`
- Modify: `src/assistant_agent/agent/provider_streaming.py` only if the test reveals a runner gap.
- Modify: `src/assistant_agent/agent/runtime.py` only if the test reveals integration drift.

**Interfaces:**
- Consumes:
  - `_NativeRuntimeResponseBuffer` through existing `ChatRequest.stream_callback`
  - `LLMEventAccumulator.finalize_tool_calls(provider_format="openai_compatible")`
  - existing native tool execution loop
- Produces: no new public interface.

- [ ] **Step 1: Add streaming tool-call runtime test**

Append to `tests/test_runtime_provider_streaming.py`:

```python
def test_runtime_streaming_tool_call_does_not_emit_tool_arguments_as_response_delta() -> None:
    first_turn = [
        LLMEvent(
            event_type="token_delta",
            provider="scripted-stream",
            model="stream-model",
            text="checking",
            metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
        ),
        LLMEvent(
            event_type="tool_call_delta",
            provider="scripted-stream",
            model="stream-model",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call_1",
                type="function",
                name_delta="product_search",
                arguments_delta='{"query": "commute headphones", "limit": 2}',
            ),
        ),
        LLMEvent(
            event_type="completed",
            provider="scripted-stream",
            model="stream-model",
            finish_reason="tool_calls",
        ),
    ]
    second_turn = [
        LLMEvent(
            event_type="token_delta",
            provider="scripted-stream",
            model="stream-model",
            text="found two candidates",
            metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
        ),
        LLMEvent(
            event_type="completed",
            provider="scripted-stream",
            model="stream-model",
            finish_reason="stop",
        ),
    ]
    adapter = StreamingAndSyncChatAdapter([first_turn, second_turn])
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    )

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="find commute headphones"),
        event_sink=sink,
    )

    response_delta_texts = [event.text for event in sink.events if event.type == "response_delta"]
    progress_events = [event for event in sink.events if event.type == "progress_message"]
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert response_delta_texts == ["found two candidates"]
    assert progress_events
    assert progress_events[0].text == "我查一下。"
    assert state.request.metadata["native_tool_call_preambles"] == [
        {"tool_name": "product_search", "content": "checking"}
    ]
    assert "commute headphones" not in repr(response_delta_texts)
    assert state.response is not None
    assert state.response.message == "found two candidates"
```

- [ ] **Step 2: Run the streaming tool-call test**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_runtime_streaming_tool_call_does_not_emit_tool_arguments_as_response_delta -q
```

Expected: pass after Tasks 2 and 3. If it fails, fix only the runner/runtime bridge needed to preserve existing `_NativeRuntimeResponseBuffer` semantics.

- [ ] **Step 3: Run native tool regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py tests/test_agent_events.py -q
```

Expected: existing native tool and event tests pass.

---

### Task 5: Provider Error And Cancellation Boundaries

**Files:**
- Modify: `tests/test_runtime_provider_streaming.py`
- Modify: `src/assistant_agent/agent/provider_streaming.py` only if tests reveal a gap.

**Interfaces:**
- Consumes:
  - `LLMEvent(error)`
  - `asyncio.CancelledError`
  - existing `AgentGraphRuntime._set_native_runtime_response(...)`
- Produces: no new public interface.

- [ ] **Step 1: Add runtime provider error test**

Append to `tests/test_runtime_provider_streaming.py`:

```python
def test_runtime_streaming_provider_error_uses_existing_task_failed_behavior() -> None:
    adapter = StreamingAndSyncChatAdapter(
        [
            [
                LLMEvent(
                    event_type="error",
                    provider="scripted-stream",
                    model="stream-model",
                    error=LLMProviderError(
                        code="provider_timeout",
                        message="Chat provider request timed out.",
                        recoverable=True,
                    ),
                )
            ]
        ]
    )
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert state.status == "failed"
    assert state.errors
    assert "provider_timeout" in state.errors[-1].message
    assert sink.events[-1].type == "task_failed"
    assert sink.events[-1].error["code"] == "TASK_FAILED"
```

- [ ] **Step 2: Add runner cancellation test**

The Task 2 runner cancellation test already verifies `asyncio.CancelledError`
does not become a provider error. Keep that as the unit-level cancellation
boundary for this implementation phase.

- [ ] **Step 3: Run error/cancellation tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py::test_runtime_streaming_provider_error_uses_existing_task_failed_behavior tests/test_runtime_provider_streaming.py::test_provider_stream_runner_does_not_convert_cancelled_error_to_provider_error -q
```

Expected: both tests pass.

---

### Task 6: Regression Verification

**Files:**
- No required source changes.

**Interfaces:**
- Verifies all prior tasks.

- [ ] **Step 1: Run provider stream and adapter tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_runtime_provider_streaming.py tests/test_async_chat_stream.py tests/test_direct_chat_adapter.py tests/test_llm_events.py -q
```

Expected: all selected provider stream tests pass.

- [ ] **Step 2: Run runtime event and native tool tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_events.py tests/test_agent_runtime_stream.py tests/test_agent_runtime_cancellation.py tests/test_native_tool_call_handoff.py -q
```

Expected: runtime stream, cancellation, event mapping, and native tool handoff behavior pass.

- [ ] **Step 3: Run realtime and Gateway mapping tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_event_mapping.py tests/test_realtime_agent_backend.py tests/test_realtime_backend_types.py tests/test_gateway_session.py -q
```

Expected: realtime and Gateway-facing contracts pass without updates.

- [ ] **Step 4: Run compile and whitespace checks**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent tests
git diff --check
```

Expected: both commands pass.

- [ ] **Step 5: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: fast suite passes.

- [ ] **Step 6: Final status check**

Run:

```bash
git status --short
git diff --stat
```

Expected:

- source changes are limited to `src/assistant_agent/agent/provider_streaming.py`, `src/assistant_agent/agent/runtime.py`, and `src/assistant_agent/config.py`;
- test changes are limited to `tests/test_runtime_provider_streaming.py` plus existing provider stream tests if they need minor helper reuse;
- Gateway, realtime, TTS, UI, memory, and tool executor files are unchanged;
- no commit exists unless the user explicitly asked for one.

