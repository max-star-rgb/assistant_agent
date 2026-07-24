"""Regressions for generic foreground ReAct tool-failure recovery."""

from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.realtime.event_mapping import map_agent_event_stream
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class _RecoverableProbeTool(ToolBase):
    name = "recoverable_probe"
    description = "Read a test value. Change invalid input before retrying."
    input_schema = _ProbeInput
    output_schema = _ProbeInput
    category = "read"
    requires_confirmation = False

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def _run(self, input: _ProbeInput, context: ToolContext) -> ToolResult:
        self.inputs.append(input.value)
        if input.value != "fixed":
            message = "The provider rejected this input."
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"provider_unsupported_input: {message}",
                model_observation={
                    "summary": message,
                    "errors": [
                        {
                            "code": "provider_unsupported_input",
                            "message": message,
                            "recoverable": True,
                        }
                    ],
                },
                output_ref="probe://failed",
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
            model_observation={"summary": "The corrected input succeeded."},
            output_ref="probe://fixed",
        )


class _SideEffectProbeTool(_RecoverableProbeTool):
    category = "write"


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-tool-recovery"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def _tool_call(call_id: str, value: str) -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="scripted-tool-recovery",
        finish_reason="tool_calls",
        tool_calls=[
            NativeToolCall(
                id=call_id,
                name="recoverable_probe",
                arguments={"value": value},
            )
        ],
    )


def _final_answer(message: str) -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="scripted-tool-recovery",
        finish_reason="stop",
        response_text=message,
    )


def _runtime(
    tool: _RecoverableProbeTool,
    adapter: _ScriptedChatAdapter,
    sink: ListEventSink | None = None,
) -> AgentGraphRuntime:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        event_sink=sink,
    )


def _request() -> UserRequest:
    return UserRequest(
        user_id="tool-recovery-user",
        session_id="tool-recovery-session",
        text="input-sentinel",
    )


def test_tool_executor_failure_mode_controls_run_state_without_tool_specific_rules() -> None:
    registry = ToolRegistry()
    registry.register(_RecoverableProbeTool())
    executor = ToolExecutor(registry=registry)
    strict_state = AgentState.from_request(_request())
    react_state = AgentState.from_request(_request())

    executor.run_tool(
        strict_state,
        "strict-step",
        "recoverable_probe",
        {"value": "bad"},
    )
    executor.run_tool(
        react_state,
        "react-step",
        "recoverable_probe",
        {"value": "bad"},
        failure_mode="continue_to_model",
    )

    assert strict_state.status == "failed"
    assert strict_state.errors[0].details["recovery_action"] == "stop_with_error"
    assert react_state.status == "running"
    assert react_state.errors[0].details["recovery_action"] == "continue_to_model"


def test_failed_tool_result_returns_to_llm_and_completes_with_degraded_answer() -> None:
    tool = _RecoverableProbeTool()
    adapter = _ScriptedChatAdapter(
        [
            _tool_call("probe-call-1", "bad"),
            _final_answer("final-sentinel"),
        ]
    )
    sink = ListEventSink()

    state = _runtime(tool, adapter, sink).run_state(_request())

    assert state.status == "completed"
    assert tool.inputs == ["bad"]
    assert state.response is not None
    assert state.response.message
    assert state.response.data["degraded"] is True
    assert state.response.data["handled_tool_failures"] == 1
    assert state.errors[0].details["recovery_action"] == "continue_to_model"
    assert any(event.type == "final_response" for event in sink.events)
    assert not any(event.type == "task_failed" for event in sink.events)
    realtime_events = [
        realtime_event
        for event in sink.events
        for realtime_event in map_agent_event_stream(event)
    ]
    assert any(event.type == "response.final" for event in realtime_events)
    assert not any(event.type == "error" for event in realtime_events)
    gateway_frames = [
        frame
        for event in realtime_events
        if (
            frame := realtime_event_to_frame(
                event,
                session_id="tool-recovery-session",
                turn_id="tool-recovery-turn",
                run_id=state.run_id,
            )
        )
        is not None
    ]
    assert not any(frame["type"] == "event.error" for frame in gateway_frames)


def test_failed_tool_call_allows_retry_with_changed_arguments() -> None:
    tool = _RecoverableProbeTool()
    adapter = _ScriptedChatAdapter(
        [
            _tool_call("probe-call-1", "bad"),
            _tool_call("probe-call-2", "fixed"),
            _final_answer("final-sentinel"),
        ]
    )

    request = _request()
    request.metadata["tool_visibility"] = {
        "enabled_tools": ["recoverable_probe"]
    }

    state = _runtime(tool, adapter).run_state(request)

    assert state.status == "completed"
    assert tool.inputs == ["bad", "fixed"]
    assert [result.success for result in state.tool_results] == [False, True]
    assert state.response is not None
    assert state.response.message


def test_identical_failed_tool_call_is_blocked_then_forces_answer_only_turn() -> None:
    tool = _RecoverableProbeTool()
    adapter = _ScriptedChatAdapter(
        [
            _tool_call("probe-call-1", "bad"),
            _tool_call("probe-call-2", "bad"),
            _final_answer("final-sentinel"),
        ]
    )

    state = _runtime(tool, adapter).run_state(_request())

    assert state.status == "completed"
    assert tool.inputs == ["bad"]
    assert len(state.tool_calls) == 1
    assert adapter.requests[2].tools == []
    assert state.request.metadata["assistant_answer_only_reason"] == "duplicate_failed_tool_call"
    assert state.response is not None
    assert state.response.message


def test_failed_side_effect_tool_forces_answer_only_without_retry() -> None:
    tool = _SideEffectProbeTool()
    adapter = _ScriptedChatAdapter(
        [
            _tool_call("probe-call-1", "bad"),
            _final_answer("final-sentinel"),
        ]
    )

    request = _request()
    request.metadata["tool_visibility"] = {
        "enabled_tools": ["recoverable_probe"]
    }

    state = _runtime(tool, adapter).run_state(request)

    assert state.status == "completed"
    assert tool.inputs == ["bad"]
    assert adapter.requests[1].tools == []
    assert state.request.metadata["assistant_answer_only_reason"] == "failed_side_effect_tool"
