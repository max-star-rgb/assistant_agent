from __future__ import annotations

import importlib

import pytest

from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.gateway.runtime_event_mapping import map_agent_event_stream
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.chat_adapter import ChatProviderError, ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    CancelledToken,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _RunLifecycleProbeTool(ProbeTool):
    name = "run_lifecycle_probe_tool"

    def __init__(self) -> None:
        self.terminals: list[tuple[str, str]] = []

    def on_run_terminal(self, run_id: str, status: str) -> None:
        self.terminals.append((run_id, status))


@pytest.fixture(autouse=True)
def default_registry_assembly_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_default_registry(*args, **kwargs):
        raise AssertionError("default-registry-called")

    monkeypatch.setattr(
        "assistant_agent.runtime.runtime.create_default_registry",
        reject_default_registry,
    )


@pytest.mark.core_invariant("BOOT-001")
def test_runtime_initializes_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
    )
    try:
        assert package is not None
        assert runtime.config.provider_mode == "mock"
        assert runtime.chat_adapter.provider == "mock"
        assert runtime.registry.sealed is True
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_plain_text_run_reaches_completed_terminal_state() -> None:
    sink = ListEventSink()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message
        assert sink.events[0].type == "task_started"
        assert sink.events[-1].type == "final_response"
        trace_events = runtime.trace_store.list_by_run(state.run_id)
        run_started = next(
            event for event in trace_events if event.canonical_event == "run.started"
        )
        run_completed = next(
            event
            for event in trace_events
            if event.canonical_event == "run.completed"
        )
        assert sink.events[0].created_at == run_started.created_at
        assert sink.events[-1].created_at == run_completed.created_at
    finally:
        runtime.close()


@pytest.mark.core_invariant("IDENT-001")
def test_entry_run_and_agent_identity_are_preserved() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        agent_id="agent-sentinel",
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-sentinel",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            run_id="run-sentinel",
        )

        assert state.run_id == "run-sentinel"
        assert state.agent_id == "agent-sentinel"
        assert {
            event.run_id
            for event in runtime.trace_store.list_by_run("run-sentinel")
        } == {"run-sentinel"}
    finally:
        runtime.close()


@pytest.mark.core_invariant("TOOL-001")
def test_probe_tool_call_completes_through_governed_runtime() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": "value-sentinel"},
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
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert [call.tool_name for call in state.tool_calls] == [ProbeTool.name]
        assert len(state.tool_results) == 1
        assert state.tool_results[0].success is True
        assert state.tool_results[0].data == {"value": "value-sentinel"}
        trace_events = runtime.trace_store.list_by_run(state.run_id)
        terminal = next(
            event
            for event in trace_events
            if event.canonical_event == "tool.finished"
        )
        observation = next(
            event
            for event in trace_events
            if event.canonical_event == "tool.observation"
        )
        assert (
            observation.attributes["tool_call_id"]
            == terminal.attributes["tool_call_id"]
        )
        assert observation.attributes["source_tool_span_id"] == terminal.span_id
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
def test_provider_timeout_returns_structured_terminal_reason() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                errors=[
                    ChatProviderError(
                        code="provider_timeout",
                        message="error-sentinel",
                        recoverable=True,
                    )
                ],
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.data["fallback_reason"] == "provider_timeout"
        assert state.response.message
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_cancelled_run_emits_no_final_response() -> None:
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
            cancel_token=CancelledToken(),
        )

        assert state.status == "cancelled"
        assert state.response is None
        assert sink.events[-1].type == "task_cancelled"
        assert "final_response" not in [event.type for event in sink.events]
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.parametrize("expected_status", ["completed", "failed", "cancelled"])
def test_runtime_notifies_optional_tool_lifecycle_at_every_run_terminal(
    expected_status: str,
) -> None:
    tool = _RunLifecycleProbeTool()
    registry = sealed_registry(tool)
    runtime = AgentGraphRuntime(
        registry=registry,
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-sentinel",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
                task_execution_mode=(
                    "durable" if expected_status == "failed" else "auto"
                ),
            ),
            run_id="run-terminal-sentinel",
            cancel_token=(
                CancelledToken() if expected_status == "cancelled" else None
            ),
        )

        assert state.status == expected_status
        assert tool.terminals == [("run-terminal-sentinel", expected_status)]
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_registry_terminal_lifecycle_accepts_failed_terminal_status() -> None:
    tool = _RunLifecycleProbeTool()
    registry = sealed_registry(tool)

    issues = registry.notify_run_terminal("run-failed-sentinel", "failed")

    assert issues == []
    assert tool.terminals == [("run-failed-sentinel", "failed")]


@pytest.mark.core_invariant("OBS-001")
def test_core_event_reaches_gateway_frame() -> None:
    realtime_events = map_agent_event_stream(
        AgentEvent(
            type="final_response",
            session_id="session-sentinel",
            run_id="run-sentinel",
            text="value-sentinel",
        )
    )
    frame = realtime_event_to_frame(
        realtime_events[0],
        session_id="session-sentinel",
        turn_id="turn-sentinel",
        run_id="run-sentinel",
    )

    assert frame is not None
    assert frame["type"] == "stream.chunk"
    assert frame["session_id"] == "session-sentinel"
    assert frame["run_id"] == "run-sentinel"
    assert frame["payload"]["text"] == "value-sentinel"


@pytest.mark.core_invariant("IDENT-001")
def test_user_session_runs_are_isolated_and_request_identity_fields_are_preserved() -> None:
    sessions = InMemorySessionStore()
    sessions.touch_run(
        user_id="user-a-sentinel",
        session_id="session-sentinel",
        run_id="run-a-sentinel",
        trace_id="trace-a-sentinel",
        message_preview="value-a-sentinel",
        status="completed",
    )
    sessions.touch_run(
        user_id="user-b-sentinel",
        session_id="session-sentinel",
        run_id="run-b-sentinel",
        trace_id="trace-b-sentinel",
        message_preview="value-b-sentinel",
        status="completed",
    )

    session_a = sessions.get("user-a-sentinel", "session-sentinel")
    session_b = sessions.get("user-b-sentinel", "session-sentinel")
    user_a_identity = RequestIdentity.for_user(
        user_id="user-a-sentinel",
        agent_id="agent-a-sentinel",
        session_id="session-sentinel",
    )
    other_agent_identity = RequestIdentity.for_user(
        user_id="user-a-sentinel",
        agent_id="agent-b-sentinel",
        session_id="session-sentinel",
    )

    assert session_a is not None
    assert session_b is not None
    assert session_a.last_run_id == "run-a-sentinel"
    assert session_b.last_run_id == "run-b-sentinel"
    assert [
        record.last_run_id
        for record in sessions.list_by_user("user-a-sentinel")
    ] == ["run-a-sentinel"]
    assert [
        record.last_run_id
        for record in sessions.list_by_user("user-b-sentinel")
    ] == ["run-b-sentinel"]
    assert user_a_identity.model_dump() == {
        "user_id": "user-a-sentinel",
        "agent_id": "agent-a-sentinel",
        "session_id": "session-sentinel",
    }
    assert other_agent_identity.model_dump() == {
        "user_id": "user-a-sentinel",
        "agent_id": "agent-b-sentinel",
        "session_id": "session-sentinel",
    }
