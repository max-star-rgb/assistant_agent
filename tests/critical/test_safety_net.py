"""默认离线安全网：只保护少量稳定、跨层且高风险的运行边界。"""

from datetime import datetime, timezone
import importlib

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.realtime.event_mapping import map_agent_event_stream
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.llm_events import (
    LLMEvent,
    LLMEventAccumulator,
    LLMToolCallDelta,
)
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    MockChatAdapter,
)
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.session_store import InMemorySessionStore


def _offline_config() -> ProviderConfig:
    return ProviderConfig(langgraph_checkpointer_backend="none")


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _CancelledToken:
    def is_cancelled(self) -> bool:
        return True


def test_package_and_runtime_initialize_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(config=_offline_config())
    try:
        assert package is not None
        assert runtime.chat_adapter.provider == "mock"
        assert runtime.registry.sealed is True
        assert runtime.registry.get("memory_search") is not None
    finally:
        runtime.close()


def test_plain_text_run_completes() -> None:
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=MockChatAdapter(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="safety-user",
                session_id="safety-session",
                text="input-sentinel",
            ),
            event_sink=sink,
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message
        assert sink.events[0].type == "task_started"
        assert sink.events[-1].type == "final_response"
    finally:
        runtime.close()


def test_provider_native_tool_call_completes_through_governed_runtime() -> None:
    memory_store = InMemoryStore()
    memory_store.save(
        MemoryItem(
            memory_id="memory-sentinel",
            user_id="tool-user",
            memory_type="preference",
            content={"value": "tool-observation-sentinel"},
            summary="tool-observation-sentinel",
            created_at=datetime.now(timezone.utc),
        )
    )
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-sentinel",
                        name="memory_search",
                        arguments={"query": "tool-observation-sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=memory_store,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="tool-user",
                session_id="tool-session",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert len(adapter.requests) == 2
        assert [call.tool_name for call in state.tool_calls] == ["memory_search"]
        assert len(state.tool_results) == 1
        assert state.tool_results[0].success is True
        assert state.tool_results[0].tool_name == "memory_search"
    finally:
        runtime.close()


def test_provider_timeout_returns_terminal_retry_response() -> None:
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                errors=[
                    ChatProviderError(
                        code="provider_timeout",
                        message="timeout-sentinel",
                        recoverable=True,
                    )
                ],
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="timeout-user",
                session_id="timeout-session",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.data["fallback_reason"] == "provider_timeout"
        assert state.response.message
    finally:
        runtime.close()


def test_cancelled_run_terminates_without_final_response() -> None:
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="cancel-user",
                session_id="cancel-session",
                text="input-sentinel",
            ),
            event_sink=sink,
            cancel_token=_CancelledToken(),
        )

        assert state.status == "cancelled"
        assert state.response is None
        assert sink.events[-1].type == "task_cancelled"
    finally:
        runtime.close()


def test_core_event_contract_reaches_gateway_frame() -> None:
    accumulator = LLMEventAccumulator()
    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="scripted",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call-sentinel",
                name_delta="memory_search",
                arguments_delta='{"query":"query-sentinel"}',
            ),
        )
    )
    assert accumulator.finalize_tool_calls()[0].arguments == {
        "query": "query-sentinel"
    }

    realtime_events = map_agent_event_stream(
        AgentEvent(
            type="final_response",
            session_id="frame-session",
            run_id="frame-run",
            text="frame-payload-sentinel",
        )
    )
    frame = realtime_event_to_frame(
        realtime_events[0],
        session_id="frame-session",
        turn_id="frame-turn",
        run_id="frame-run",
    )

    assert frame is not None
    assert frame["type"] == "stream.chunk"
    assert frame["session_id"] == "frame-session"
    assert frame["run_id"] == "frame-run"
    assert frame["payload"]["text"] == "frame-payload-sentinel"


def test_session_and_memory_are_isolated_by_user_identity() -> None:
    sessions = InMemorySessionStore()
    sessions.touch_run(
        user_id="identity-user-a",
        session_id="shared-session",
        run_id="run-a",
        trace_id="trace-a",
        message_preview="preview-a",
        status="completed",
    )
    sessions.touch_run(
        user_id="identity-user-b",
        session_id="shared-session",
        run_id="run-b",
        trace_id="trace-b",
        message_preview="preview-b",
        status="completed",
    )

    memories = InMemoryStore()
    memories.save(
        MemoryItem(
            memory_id="identity-memory",
            user_id="identity-user-a",
            session_id="shared-session",
            memory_type="preference",
            summary="identity-memory-sentinel",
            content={"value": "identity-memory-sentinel"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    session_a = sessions.get("identity-user-a", "shared-session")
    session_b = sessions.get("identity-user-b", "shared-session")
    assert session_a is not None and session_a.last_run_id == "run-a"
    assert session_b is not None and session_b.last_run_id == "run-b"
    assert (
        memories.search(
            MemoryQuery(user_id="identity-user-a", query="identity-memory-sentinel")
        ).total
        == 1
    )
    assert (
        memories.search(
            MemoryQuery(user_id="identity-user-b", query="identity-memory-sentinel")
        ).total
        == 0
    )
