import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest

from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator
from assistant_agent.services.chat_adapter import (
    AsyncStreamingChatAdapter,
    ChatRequest,
    MockChatAdapter,
    OpenAICompatibleChatAdapter,
)


def chat_request(text: str = "explain Agent") -> ChatRequest:
    return ChatRequest(user_id="u1", session_id="s1", user_query=text)


def run_async_test(test_func):
    def wrapper(*args, **kwargs):
        return asyncio.run(test_func(*args, **kwargs))

    return wrapper


async def collect_events(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


class FakeAsyncStream:
    def __init__(self, chunks: list[dict[str, Any]], error: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self.error = error
        self.closed = False

    def __aiter__(self) -> "FakeAsyncStream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._chunks:
            return self._chunks.pop(0)
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncCompletions:
    def __init__(self, *, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
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


class CancellingAsyncCompletions(FakeAsyncCompletions):
    async def create(self, **payload: Any) -> Any:
        self.calls.append(payload)
        raise asyncio.CancelledError()


def async_adapter(
    *,
    response: Any = None,
    error: BaseException | None = None,
) -> tuple[OpenAICompatibleChatAdapter, FakeAsyncCompletions]:
    completions = FakeAsyncCompletions(response=response, error=error)
    adapter = OpenAICompatibleChatAdapter(
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        async_client=FakeAsyncSDKClient(completions),
    )
    return adapter, completions


@run_async_test
async def test_mock_stream_chat_returns_async_iterator_without_awaiting() -> None:
    adapter: AsyncStreamingChatAdapter = MockChatAdapter()

    stream = adapter.stream_chat(chat_request())

    assert inspect.isawaitable(stream) is False
    assert hasattr(stream, "__aiter__")
    events = await collect_events(stream)
    assert [event.event_type for event in events] == ["token_delta", "completed"]


@run_async_test
async def test_mock_stream_chat_emits_token_delta_then_completed() -> None:
    events = await collect_events(MockChatAdapter().stream_chat(chat_request("hello")))

    token, completed = events
    assert token.event_type == "token_delta"
    assert token.provider == "mock"
    assert token.model == "mock-direct-chat"
    assert token.text is not None
    assert "hello" in token.text
    assert token.finish_reason is None
    assert token.metadata == {
        "token_streaming": False,
        "chunking_strategy": "mock_full_text",
    }
    assert completed.event_type == "completed"
    assert completed.provider == "mock"
    assert completed.model == "mock-direct-chat"
    assert completed.finish_reason == "stop"
    assert completed.usage["input_chars"] == len("hello")


@run_async_test
async def test_mock_stream_chat_does_not_call_stream_callback() -> None:
    callback_events: list[tuple[str, dict[str, Any]]] = []

    events = await collect_events(
        MockChatAdapter().stream_chat(
            ChatRequest(
                user_id="u1",
                session_id="s1",
                user_query="hello",
                stream_callback=lambda text, payload: callback_events.append((text, payload)),
            )
        )
    )

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    assert callback_events == []


@run_async_test
async def test_openai_async_stream_maps_text_and_completion() -> None:
    adapter, completions = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [{"delta": {"content": "real"}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"content": " reply"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert completions.calls[0]["stream"] is True
    assert [event.event_type for event in events] == ["token_delta", "token_delta", "completed"]
    assert [event.text for event in events[:2]] == ["real", " reply"]
    assert all(event.finish_reason is None for event in events[:2])
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == {"prompt_tokens": 4, "completion_tokens": 2}


@run_async_test
async def test_openai_async_stream_does_not_call_stream_callback() -> None:
    callback_events: list[tuple[str, dict[str, Any]]] = []
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ]
        )
    )

    events = await collect_events(
        adapter.stream_chat(
            ChatRequest(
                user_id="u1",
                session_id="s1",
                user_query="hello",
                stream_callback=lambda text, payload: callback_events.append((text, payload)),
            )
        )
    )

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    assert callback_events == []


@run_async_test
async def test_openai_async_stream_maps_interleaved_tool_call_deltas() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "delta": {
                                "content": "checking",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "product_",
                                            "arguments": '{"query": "commute',
                                        },
                                    },
                                    {
                                        "index": 1,
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query": "news"}',
                                        },
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
                                        "function": {
                                            "name": "search",
                                            "arguments": ' headphones"}',
                                        },
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

    events = await collect_events(adapter.stream_chat(chat_request("find products")))

    assert [event.event_type for event in events] == [
        "token_delta",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "completed",
    ]
    assert events[0].text == "checking"
    assert events[0].finish_reason is None
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
    assert [call.name for call in calls] == ["shopping_search", "web_search"]
    assert calls[0].arguments == {"query": "commute headphones"}
    assert calls[1].arguments == {"query": "news"}


@run_async_test
async def test_openai_async_stream_provider_error_is_terminal() -> None:
    adapter, _ = async_adapter(error=TimeoutError("provider timed out"))

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert [event.event_type for event in events] == ["error"]
    assert events[0].error is not None
    assert events[0].error.code == "provider_timeout"
    assert events[0].error.recoverable is True


@run_async_test
async def test_openai_async_stream_partial_failure_yields_error_and_closes_stream() -> None:
    fake_stream = FakeAsyncStream(
        [
            {"choices": [{"delta": {"content": "first"}, "finish_reason": None}]},
        ],
        error=TimeoutError("provider timed out"),
    )
    adapter, _ = async_adapter(response=fake_stream)

    events = await collect_events(adapter.stream_chat(chat_request("hello")))

    assert [event.event_type for event in events] == ["token_delta", "error"]
    assert events[0].text == "first"
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_timeout"
    assert fake_stream.closed is True


@run_async_test
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


@run_async_test
async def test_openai_async_stream_emits_no_event_after_completed() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ]
        )
    )

    stream = adapter.stream_chat(chat_request("hello"))
    events = [event async for event in stream]

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@run_async_test
async def test_openai_async_stream_excludes_raw_provider_objects_from_events() -> None:
    adapter, _ = async_adapter(
        response=FakeAsyncStream(
            [
                {
                    "model": "deepseek-chat",
                    "raw_secret": "must_not_leak",
                    "headers": {"authorization": "Bearer sk-test"},
                    "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                },
            ]
        )
    )

    events = await collect_events(adapter.stream_chat(chat_request("DO_NOT_LEAK_PROMPT")))
    rendered = "\n".join(repr(event) for event in events)
    payloads = [event.model_dump(mode="json") for event in events]

    assert "must_not_leak" not in rendered
    assert "sk-test" not in rendered
    assert "DO_NOT_LEAK_PROMPT" not in rendered
    assert "must_not_leak" not in repr(payloads)
    assert "sk-test" not in repr(payloads)
    assert "DO_NOT_LEAK_PROMPT" not in repr(payloads)


@run_async_test
async def test_openai_async_stream_provider_error_is_prompt_safe() -> None:
    adapter, _ = async_adapter(error=TimeoutError("DO_NOT_LEAK_PROMPT sk-test Authorization"))

    events = await collect_events(adapter.stream_chat(chat_request("DO_NOT_LEAK_PROMPT")))

    assert [event.event_type for event in events] == ["error"]
    rendered = repr(events[0])
    payload = events[0].model_dump(mode="json")
    assert "DO_NOT_LEAK_PROMPT" not in rendered
    assert "sk-test" not in rendered
    assert "Authorization" not in rendered
    assert "DO_NOT_LEAK_PROMPT" not in repr(payload)
    assert "sk-test" not in repr(payload)
    assert "Authorization" not in repr(payload)


@run_async_test
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
