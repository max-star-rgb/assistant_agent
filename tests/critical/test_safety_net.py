"""默认离线安全网：只保护少量稳定、跨层且高风险的运行边界。"""

import importlib

from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.memory.mem0.base import bind_mem0_identity
from assistant_agent.memory.mem0.store import Mem0MemoryStore
from assistant_agent.realtime.event_mapping import map_agent_event_stream
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.llm_events import (
    LLMEvent,
    LLMEventAccumulator,
    LLMToolCallDelta,
)
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    MockChatAdapter,
)
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


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


class _ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class _ProbeTool(ToolBase):
    name = "probe_tool"
    description = "Return a test value."
    input_schema = _ProbeInput
    output_schema = _ProbeInput
    category = "read"
    requires_confirmation = False

    def _run(self, input: _ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


def test_package_and_runtime_initialize_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(config=_offline_config())
    try:
        assert package is not None
        assert runtime.chat_adapter.provider == "mock"
        assert runtime.registry.sealed is True
        assert "memory_search" not in runtime.registry.list()
        assert isinstance(runtime.memory_store, Mem0MemoryStore)
        assert runtime.memory_store.supports_turn_capture is False
    finally:
        runtime.close()


def test_plain_text_run_completes() -> None:
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=MockChatAdapter(),
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


def test_runtime_preserves_entry_run_id_and_agent_identity() -> None:
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        agent_id="agent.worker",
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="identity-user",
                session_id="identity-session",
                text="hello",
            ),
            run_id="run-entry-owned",
        )

        assert state.run_id == "run-entry-owned"
        assert state.agent_id == "agent.worker"
        assert {
            event.run_id for event in runtime.trace_store.list_by_run("run-entry-owned")
        } == {"run-entry-owned"}
    finally:
        runtime.close()


def test_provider_native_tool_call_completes_through_governed_runtime() -> None:
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-sentinel",
                        name="probe_tool",
                        arguments={"value": "tool-observation-sentinel"},
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
        registry=registry,
        config=_offline_config(),
        chat_adapter=adapter,
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
        assert [call.tool_name for call in state.tool_calls] == ["probe_tool"]
        assert len(state.tool_results) == 1
        assert state.tool_results[0].success is True
        assert state.tool_results[0].tool_name == "probe_tool"
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
                name_delta="probe_tool",
                arguments_delta='{"value":"query-sentinel"}',
            ),
        )
    )
    assert accumulator.finalize_tool_calls()[0].arguments == {
        "value": "query-sentinel"
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


def test_session_and_mem0_identity_are_isolated_by_user_identity() -> None:
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

    session_a = sessions.get("identity-user-a", "shared-session")
    session_b = sessions.get("identity-user-b", "shared-session")
    assert session_a is not None and session_a.last_run_id == "run-a"
    assert session_b is not None and session_b.last_run_id == "run-b"
    identity_a = bind_mem0_identity(
        RequestIdentity.for_user(
            user_id="identity-user-a",
            session_id="shared-session",
        ),
        namespace="test",
    )
    identity_b = bind_mem0_identity(
        RequestIdentity.for_user(
            user_id="identity-user-b",
            session_id="shared-session",
        ),
        namespace="test",
    )
    other_agent_identity = bind_mem0_identity(
        RequestIdentity.for_user(
            user_id="identity-user-a",
            agent_id="agent.other",
            session_id="shared-session",
        ),
        namespace="test",
    )
    assert identity_a.user_id != identity_b.user_id
    assert identity_a.agent_id == identity_b.agent_id
    assert identity_a.user_id == other_agent_identity.user_id
    assert identity_a.agent_id != other_agent_identity.agent_id
    assert identity_a.run_id != identity_b.run_id
    assert identity_a.run_id != other_agent_identity.run_id
