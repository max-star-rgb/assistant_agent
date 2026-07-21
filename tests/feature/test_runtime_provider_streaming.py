from collections.abc import AsyncIterator

import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.llm_events import LLMEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import InMemoryTraceStore


class StreamingChatAdapter:
    provider = "qwen"
    model = "qwen3.7-max"

    async def stream_chat(self, _request: ChatRequest) -> AsyncIterator[LLMEvent]:
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
        memory_store=InMemoryStore(),
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
    deltas = [event for event in event_sink.events if event.type == "response_delta"]
    assert [event.text for event in deltas] == ["你好，", "我是你的助理。"]
    assert all(event.payload["token_streaming"] is True for event in deltas)
