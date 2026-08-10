from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[
            tuple[ToolRegistry, str, BaseModel | dict[str, Any], ToolContext]
        ] = []

    def run(
        self,
        registry: ToolRegistry,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        self.calls.append((registry, tool_name, tool_input, context))
        return ToolResult(
            tool_name=tool_name,
            success=True,
            data={"sentinel": 1},
        )


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )


def test_custom_backend_preserves_executor_governance() -> None:
    backend = RecordingBackend()
    registry = sealed_registry()
    events = ListEventSink()
    state = AgentState.from_request(_request())

    result = ToolExecutor(
        registry=registry,
        event_sink=events,
        execution_backend=backend,
    ).run_tool(
        state,
        "step-sentinel",
        ProbeTool.name,
        {"value": "value-sentinel"},
    )

    received_registry, tool_name, tool_input, context = backend.calls[0]
    assert received_registry is registry
    assert received_registry.sealed is True
    assert tool_name == ProbeTool.name
    assert tool_input == {"value": "value-sentinel"}
    assert context.user_id == "user-sentinel"
    assert context.session_id == "session-sentinel"
    assert result.data == {"sentinel": 1}
    assert state.tool_calls[0].status == "succeeded"
    assert state.tool_results == [result]
    assert [event.type for event in events.events] == ["tool_started", "tool_finished"]


def test_runtime_injects_backend_into_per_run_executor() -> None:
    backend = RecordingBackend()
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
        tool_execution_backend=backend,
    )
    try:
        state = runtime.run_state(_request())

        assert len(backend.calls) == 1
        assert state.tool_results[0].data == {"sentinel": 1}
        assert state.status == "completed"
    finally:
        runtime.close()
