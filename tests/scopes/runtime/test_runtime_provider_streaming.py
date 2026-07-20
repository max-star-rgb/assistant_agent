import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from assistant_agent.agent.provider_streaming import (
    ProviderStreamingTurnRunner,
    supports_async_streaming_chat,
)
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.llm_events import LLMEvent, LLMProviderError, LLMToolCallDelta
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult
from assistant_agent.services.event_sink import ListEventSink


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


class StreamingAndSyncChatAdapter(ScriptedStreamingChatAdapter):
    def __init__(self, scripts: list[list[LLMEvent]], sync_result: ChatResult) -> None:
        super().__init__(scripts)
        self.sync_result = sync_result
        self.chat_calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        self.chat_calls += 1
        self.requests.append(request)
        return self.sync_result


def chat_request(callback=None) -> ChatRequest:
    return ChatRequest(
        user_id="u1",
        session_id="s1",
        user_query="hello",
        stream_callback=callback,
    )


def test_native_provider_streaming_defaults_to_disabled() -> None:
    config = ProviderConfig.from_env({})

    assert config.native_provider_streaming is False


def test_native_provider_streaming_env_flag_enables_runtime_stream_path() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "1"})

    assert config.native_provider_streaming is True


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
    assert result.tool_calls[0].name == "shopping_search"
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


def test_provider_stream_runner_converts_empty_terminal_response() -> None:
    adapter = ScriptedStreamingChatAdapter(
        [
            [
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                )
            ]
        ]
    )

    result = ProviderStreamingTurnRunner().run_turn(adapter, chat_request())

    assert result.success is False
    assert result.provider == "scripted-stream"
    assert result.model == "stream-model"
    assert result.errors[0].code == "provider_empty_response"
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


def test_runtime_uses_sync_chat_when_native_provider_streaming_disabled() -> None:
    adapter = StreamingAndSyncChatAdapter(
        [
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="stream answer",
                ),
                LLMEvent(event_type="completed", provider="scripted-stream", model="stream-model"),
            ]
        ],
        ChatResult(
            response_text="sync answer",
            finish_reason="stop",
            message_kind="final_answer",
            provider="scripted-sync",
            model="sync-model",
        ),
    )
    sink = ListEventSink()

    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=False),
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert adapter.chat_calls == 1
    assert len(adapter.scripts) == 1
    assert state.response is not None
    assert state.response.message == "sync answer"
    assert [event.text for event in sink.events if event.type == "response_delta"] == ["sync answer"]


def test_runtime_streaming_direct_final_answer_emits_agent_response_delta() -> None:
    adapter = StreamingAndSyncChatAdapter(
        [
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="stream ",
                ),
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="answer",
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                    usage={"completion_tokens": 2},
                ),
            ]
        ],
        ChatResult(
            response_text="sync answer",
            finish_reason="stop",
            message_kind="final_answer",
            provider="scripted-sync",
            model="sync-model",
        ),
    )
    sink = ListEventSink()

    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert adapter.chat_calls == 0
    assert state.response is not None
    assert state.response.message == "stream answer"
    assert [event.text for event in sink.events if event.type == "response_delta"] == ["stream ", "answer"]
    assert state.provider_budget.call_records[-1].provider == "scripted-stream"
    assert state.provider_budget.call_records[-1].capability == "direct_chat"


def test_runtime_streaming_tool_call_runs_through_tool_chain_without_argument_delta_leak() -> None:
    adapter = StreamingAndSyncChatAdapter(
        [
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="checking",
                ),
                LLMEvent(
                    event_type="tool_call_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    tool_call_delta=LLMToolCallDelta(
                        index=0,
                        id="call_1",
                        type="function",
                        name_delta="shopping_",
                        arguments_delta='{"query": "通勤',
                    ),
                ),
                LLMEvent(
                    event_type="tool_call_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    tool_call_delta=LLMToolCallDelta(
                        index=0,
                        name_delta="search",
                        arguments_delta='耳机", "limit": 2}',
                    ),
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="tool_calls",
                ),
            ],
            [
                LLMEvent(
                    event_type="token_delta",
                    provider="scripted-stream",
                    model="stream-model",
                    text="已找到",
                ),
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                ),
            ],
        ],
        ChatResult(
            response_text="sync answer",
            finish_reason="stop",
            message_kind="final_answer",
            provider="scripted-sync",
            model="sync-model",
        ),
    )
    sink = ListEventSink()

    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"), event_sink=sink)

    response_delta_texts = [event.text for event in sink.events if event.type == "response_delta"]
    assert adapter.chat_calls == 0
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert state.request.metadata["native_tool_call_preambles"] == [
        {"tool_name": "shopping_search", "content": "checking"}
    ]
    assert response_delta_texts == ["已找到"]
    assert "通勤耳机" not in repr(response_delta_texts)
    assert state.response is not None
    assert state.response.message == "已找到"


@pytest.mark.parametrize(
    ("events", "expected_code", "expected_message"),
    [
        (
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
            ],
            "provider_timeout",
            "抱歉，刚才主模型没有及时响应，请再说一遍。",
        ),
        (
            [
                LLMEvent(
                    event_type="completed",
                    provider="scripted-stream",
                    model="stream-model",
                    finish_reason="stop",
                )
            ],
            "provider_empty_response",
            "抱歉，刚才主模型返回为空，请再说一遍。",
        ),
    ],
)
def test_runtime_streaming_recoverable_main_llm_no_answer_returns_retry_prompt(
    events: list[LLMEvent],
    expected_code: str,
    expected_message: str,
) -> None:
    adapter = StreamingAndSyncChatAdapter(
        [events],
        ChatResult(
            response_text="sync answer",
            finish_reason="stop",
            message_kind="final_answer",
            provider="scripted-sync",
            model="sync-model",
        ),
    )
    sink = ListEventSink()

    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert adapter.chat_calls == 0
    assert state.status == "completed"
    assert state.response is not None
    assert state.response.message == expected_message
    assert state.response.data["main_llm_no_answer_fallback"] is True
    assert state.response.data["errors"][0]["code"] == expected_code
    assert state.errors[-1].details["code"] == expected_code
    assert state.errors[-1].details["recoverable"] is True
    assert all(event.type != "task_failed" for event in sink.events)
    assert [event.text for event in sink.events if event.type == "response_delta"] == [
        expected_message
    ]
    assert [event.text for event in sink.events if event.type == "final_response"] == [
        expected_message
    ]


def test_runtime_sync_recoverable_main_llm_no_answer_returns_retry_prompt() -> None:
    adapter = StreamingAndSyncChatAdapter(
        [],
        ChatResult(
            response_text="",
            finish_reason="stop",
            message_kind="empty",
            provider="scripted-sync",
            model="sync-model",
            errors=[
                ChatProviderError(
                    code="provider_empty_response",
                    message="chat provider returned empty content",
                    recoverable=False,
                )
            ],
        ),
    )
    sink = ListEventSink()

    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=False),
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert adapter.chat_calls == 1
    assert state.status == "completed"
    assert state.response is not None
    assert state.response.message == "抱歉，刚才主模型返回为空，请再说一遍。"
    assert state.response.data["fallback_reason"] == "provider_empty_response"
    assert state.response.data["errors"][0]["recoverable"] is True
    assert all(event.type != "task_failed" for event in sink.events)


def test_runtime_streaming_cancelled_error_propagates_without_task_failed_event() -> None:
    sink = ListEventSink()

    with pytest.raises(asyncio.CancelledError):
        AgentGraphRuntime(
            config=ProviderConfig(native_provider_streaming=True),
            chat_adapter=CancellingStreamingChatAdapter([]),  # type: ignore[arg-type]
        ).run_state(UserRequest(user_id="u1", session_id="s1", text="hello"), event_sink=sink)

    assert [event.type for event in sink.events] == ["task_started"]
