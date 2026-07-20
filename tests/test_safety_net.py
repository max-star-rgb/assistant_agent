"""Minimal offline safety net for the assistant's stable runtime boundaries."""

from datetime import datetime, timezone
import importlib

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.system_prompt_policy import SystemPromptProfile, render_system_instruction
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.realtime.event_mapping import map_agent_event_stream
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator, LLMToolCallDelta
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult, MockChatAdapter
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.langfuse_scores import LangfuseScoreTraceObserver
from assistant_agent.services.otel_exporter import TextOtelTraceObserver
from assistant_agent.services.session_store import InMemorySessionStore


def _offline_config() -> ProviderConfig:
    return ProviderConfig(langgraph_checkpointer_backend="none")


class ScriptedChatAdapter:
    """Small provider-boundary fake that returns complete public ChatResult values."""

    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class CancelledToken:
    def is_cancelled(self) -> bool:
        return True


class TimeoutAwareLifecycleSink:
    def __init__(self) -> None:
        self.shutdown_timeout: float | None = None

    def export(self, _spans: object) -> None:
        return None

    def write_scores(self, _scores: object) -> None:
        return None

    def shutdown(self, *, timeout: float) -> bool:
        self.shutdown_timeout = timeout
        return True


def test_package_and_runtime_initialize_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(config=_offline_config())

    assert package is not None
    assert "shopping_search" in runtime.registry.list()
    assert runtime.chat_adapter.provider == "mock"


def test_trace_observer_close_propagates_timeout_budget() -> None:
    span_sink = TimeoutAwareLifecycleSink()
    score_sink = TimeoutAwareLifecycleSink()
    store = HookTraceStore(
        HookManager(
            [
                TextOtelTraceObserver(span_sink, enabled=True),
                LangfuseScoreTraceObserver(score_sink, enabled=True),
            ]
        )
    )

    assert store.close(timeout=7.5) is True
    assert span_sink.shutdown_timeout == 7.5
    assert score_sink.shutdown_timeout == 7.5


def test_plain_text_run_completes() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=MockChatAdapter(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        event_sink=sink,
    )

    assert state.status == "completed"
    assert state.response is not None and state.response.message
    assert sink.events[0].type == "task_started"
    assert sink.events[-1].type == "final_response"


def test_agent_runtime_system_prompt_is_channel_agnostic() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                message_kind="final_answer",
                response_text="你好。",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    runtime.run_state(
        UserRequest(
            user_id="user-1",
            session_id="session-1",
            text="你好",
            metadata={"system_prompt_profile": "realtime_phone", "channel": "realtime_phone"},
        )
    )

    prompt = str(adapter.requests[0].messages[0]["content"])
    assert prompt == render_system_instruction(SystemPromptProfile.TEXT_DEFAULT)
    assert {profile.value for profile in SystemPromptProfile} == {"text_default", "final_only"}
    for channel_term in ("电话", "通话", "口语", "口播", "挂断", "TTS", "WebSocket"):
        assert channel_term not in prompt


def test_native_tool_call_loop_completes_with_observation() -> None:
    tool_call = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="tool_calls",
        message_kind="tool_call",
        tool_calls=[
            NativeToolCall(
                id="call-1",
                name="shopping_search",
                arguments={"query": "通勤耳机", "limit": 2},
                raw={
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "arguments": '{"query":"通勤耳机","limit":2}',
                    },
                },
            )
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        message_kind="final_answer",
        response_text="已完成商品搜索。",
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=ScriptedChatAdapter([tool_call, final_answer]),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="帮我找通勤耳机")
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert state.response is not None
    assert state.response.message == "已完成商品搜索。"


def test_provider_timeout_returns_terminal_retry_response() -> None:
    timeout = ChatResult(
        provider="scripted",
        model="scripted-model",
        errors=[
            ChatProviderError(
                code="provider_timeout",
                message="provider timed out",
                recoverable=True,
            )
        ],
    )
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=ScriptedChatAdapter([timeout]),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(UserRequest(user_id="user-1", session_id="session-1", text="你好"))

    assert state.status == "completed"
    assert state.response is not None
    assert state.response.data["fallback_reason"] == "provider_timeout"
    assert state.response.message


def test_cancelled_run_terminates_without_final_response() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        event_sink=sink,
        cancel_token=CancelledToken(),
    )

    assert state.status == "cancelled"
    assert state.response is None
    assert sink.events[-1].type == "task_cancelled"


def test_core_event_contract_reaches_gateway_frame() -> None:
    accumulator = LLMEventAccumulator()
    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="scripted",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call-1",
                name_delta="shopping_search",
                arguments_delta='{"query":"耳机"}',
            ),
        )
    )
    assert accumulator.finalize_tool_calls()[0].arguments == {"query": "耳机"}

    realtime_events = map_agent_event_stream(
        AgentEvent(
            type="final_response",
            session_id="session-1",
            run_id="run-1",
            text="处理完成",
        )
    )
    frame = realtime_event_to_frame(
        realtime_events[0],
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert frame is not None
    assert frame["type"] == "stream.chunk"
    assert frame["session_id"] == "session-1"
    assert frame["run_id"] == "run-1"
    assert frame["payload"]["text"] == "处理完成"


def test_session_and_memory_identity_are_isolated() -> None:
    sessions = InMemorySessionStore()
    sessions.touch_run(
        user_id="user-1",
        session_id="shared-session",
        run_id="run-1",
        trace_id="trace-1",
        message_preview="first",
        status="completed",
    )
    sessions.touch_run(
        user_id="user-2",
        session_id="shared-session",
        run_id="run-2",
        trace_id="trace-2",
        message_preview="second",
        status="completed",
    )

    memories = InMemoryStore()
    memories.save(
        MemoryItem(
            memory_id="memory-1",
            user_id="user-1",
            session_id="shared-session",
            memory_type="preference",
            summary="喜欢蓝色",
            content={"preference": "蓝色"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert sessions.get("user-1", "shared-session").last_run_id == "run-1"
    assert sessions.get("user-2", "shared-session").last_run_id == "run-2"
    assert memories.search(MemoryQuery(user_id="user-1", query="蓝色")).total == 1
    assert memories.search(MemoryQuery(user_id="user-2", query="蓝色")).total == 0
