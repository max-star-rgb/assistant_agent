from collections.abc import AsyncIterator

import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.llm_events import LLMEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_conversation import get_default_trace_conversation_store
from assistant_agent.services.trace_store import InMemoryTraceStore


class StreamingChatAdapter:
    provider = "qwen"
    model = "qwen3.7-max"

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        if request.provider_request_callback is not None:
            request.provider_request_callback(
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": "native stream payload"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
            )
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text="你好，",
        )
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text="我是你的助理。",
        )
        yield LLMEvent(
            event_type="completed",
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        )


def test_qwen_enables_native_provider_streaming_by_default() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
        }
    )

    assert config.chat_stream is True
    assert config.native_provider_streaming is True


@pytest.mark.parametrize(
    "override",
    [
        {"CHAT_STREAM": "false"},
        {"MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "false"},
    ],
)
def test_qwen_native_async_consumption_can_be_explicitly_disabled(override: dict[str, str]) -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
            **override,
        }
    )

    assert config.native_provider_streaming is False


def test_native_streaming_chat_emits_llm_span_and_final_answer() -> None:
    trace_store = InMemoryTraceStore()
    event_sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=StreamingChatAdapter(),
        session_store=InMemorySessionStore(),
        trace_store=trace_store,
    )

    state = runtime.run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好，介绍你自己"),
        event_sink=event_sink,
    )

    assert state.response is not None
    assert state.response.message == "你好，我是你的助理。"
    llm_events = [event for event in trace_store.events if event.canonical_event and event.canonical_event.startswith("llm.chat.")]
    assert [event.canonical_event for event in llm_events] == ["llm.chat.started", "llm.chat.finished"]
    assert llm_events[0].span_id == llm_events[1].span_id
    assert llm_events[1].provider == "qwen"
    assert llm_events[1].model == "qwen3.7-max"
    assert llm_events[1].status == "succeeded"
    assert llm_events[1].attributes["iteration"] == 1
    assert llm_events[1].attributes["wall_latency_ms"] >= 0
    assert llm_events[1].attributes["provider_latency_ms"] >= 0
    assert llm_events[1].attributes["transport_mode"] == "provider_stream"
    assert llm_events[1].attributes["token_delta_count"] == 2
    assert llm_events[1].attributes["tool_call_delta_count"] == 0
    assert llm_events[1].attributes["terminal_seen"] is True
    assert llm_events[1].attributes["runtime_route"] == {
        "schema_version": "runtime_route_v1",
        "result_kind": "text",
        "selected_branch": "provider_content",
        "runtime_action": "final_answer",
        "tool_call_count": 0,
    }
    conversation = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        include_llm_inputs=True,
    )
    assert conversation is not None
    assert conversation.llm_inputs[0].request == {
        "model": "qwen3.7-max",
        "messages": [{"role": "user", "content": "native stream payload"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    deltas = [event for event in event_sink.events if event.type == "response_delta"]
    assert [event.text for event in deltas] == ["你好，我是你的助理。"]
    assert deltas[0].payload["chunking_strategy"] == "provider_final_text"


class ReasoningStreamingChatAdapter:
    provider = "qwen"
    model = "qwen3.7-max"

    async def stream_chat(self, _request: ChatRequest) -> AsyncIterator[LLMEvent]:
        yield LLMEvent(
            event_type="reasoning_delta",
            provider=self.provider,
            model=self.model,
            text="这是不能发给用户的内部分析。",
        )
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text="这是公开答复。",
        )
        yield LLMEvent(event_type="completed", provider=self.provider, model=self.model, finish_reason="stop")


def test_native_streaming_never_commits_reasoning_delta() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=ProviderConfig(native_provider_streaming=True),
        chat_adapter=ReasoningStreamingChatAdapter(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="回答问题"),
        event_sink=sink,
    )

    assert state.response is not None and state.response.message == "这是公开答复。"
    assert "内部分析" not in "".join(event.text or "" for event in sink.events)
