import pytest
from pydantic import BaseModel

from assistant_agent.agent.cancellation import AgentRunCancelled
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.trace_store import InMemoryTraceStore, trace_debug_summary
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class EchoInput(BaseModel):
    text: str


class EchoTool(MockTool):
    name = "echo"
    description = "Echo input for executor tests."
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text})


class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


def test_tool_executor_updates_state_for_successful_tool_call() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", "echo", {"text": "hello"})

    assert result.success is True
    assert state.tool_calls[0].tool_name == "echo"
    assert state.tool_calls[0].status == "succeeded"
    assert state.tool_results[0].data == {"text": "hello"}


def test_tool_executor_emits_canonical_tool_lifecycle_trace_for_success() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    trace_store = InMemoryTraceStore()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        "echo",
        {"text": "hello"},
        trace_store=trace_store,
        trace_id=state.trace_id,
        node_name="execute_tool",
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    started = next(event for event in events if event["canonical_event"] == "tool.started")
    finished = next(event for event in events if event["canonical_event"] == "tool.finished")

    assert result.success is True
    assert started["tool_name"] == "echo"
    assert started["status"] == "started"
    assert started["attributes"]["tool_call_id"] == state.tool_calls[0].call_id
    assert started["attributes"]["step_id"] == "step_1"
    assert started["attributes"]["risk_gate"] == "hard_gate"
    assert finished["tool_name"] == "echo"
    assert finished["status"] == "succeeded"
    assert finished["attributes"]["tool_call_id"] == state.tool_calls[0].call_id
    assert finished["attributes"]["retry_count"] == 0
    assert "hello" not in str(events)


def test_tool_executor_emits_canonical_tool_lifecycle_trace_for_failure() -> None:
    class FailingTool(EchoTool):
        name = "failing_echo"

        def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
            return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timed out")

    registry = ToolRegistry()
    registry.register(FailingTool())
    trace_store = InMemoryTraceStore()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        "failing_echo",
        {"text": "hello"},
        trace_store=trace_store,
        trace_id=state.trace_id,
        node_name="execute_tool",
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events]
    failed = next(event for event in events if event["canonical_event"] == "tool.failed")

    assert result.success is False
    assert canonical == ["tool.started", "tool.failed"]
    assert failed["tool_name"] == "failing_echo"
    assert failed["status"] == "failed"
    assert failed["error_code"] == "provider_timeout"
    assert failed["attributes"]["tool_call_id"] == state.tool_calls[0].call_id
    assert failed["attributes"]["retry_count"] == 1
    assert "timed out" in failed["error_message"]


def test_tool_executor_pre_cancel_skips_tool_execution() -> None:
    class RecordingTool(EchoTool):
        def __init__(self) -> None:
            self.called = False

        def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
            self.called = True
            return super()._run(input, context)

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    with pytest.raises(AgentRunCancelled) as exc_info:
        ToolExecutor(registry=registry, cancel_token=MutableCancelToken(cancelled=True)).run_tool(
            state,
            "step_1",
            "echo",
            {"text": "hello"},
        )

    assert tool.called is False
    assert state.status == "created"
    assert state.tool_calls == []
    assert exc_info.value.details["cancel_phase"] == "before_tool"
    assert exc_info.value.details["tool_name"] == "echo"


def test_tool_context_exposes_run_cancel_token() -> None:
    class ContextProbeTool(EchoTool):
        def __init__(self) -> None:
            self.context_cancelled: bool | None = None
            self.context_token: MutableCancelToken | None = None

        def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
            self.context_cancelled = context.is_cancelled()
            self.context_token = context.cancel_token
            return super()._run(input, context)

    token = MutableCancelToken(cancelled=False)
    tool = ContextProbeTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    ToolExecutor(registry=registry, cancel_token=token).run_tool(state, "step_1", "echo", {"text": "hello"})

    assert tool.context_cancelled is False
    assert tool.context_token is token


def test_tool_executor_after_tool_attempt_cancel_records_deadline_metadata() -> None:
    token = MutableCancelToken(
        metadata={
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
            "deadline_ms": 40,
        }
    )

    class CancellingTool(EchoTool):
        def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
            token.cancelled = True
            return ToolResult(tool_name=self.name, success=True, data={"text": input.text})

    registry = ToolRegistry()
    registry.register(CancellingTool())
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    with pytest.raises(AgentRunCancelled) as exc_info:
        ToolExecutor(registry=registry, cancel_token=token).run_tool(
            state,
            "step_1",
            "echo",
            {"text": "hello"},
        )

    details = exc_info.value.details
    assert details["cancel_phase"] == "after_tool_attempt"
    assert details["cancel_source"] == "deadline"
    assert details["cancel_reason"] == "run_deadline_expired"
    assert details["deadline_ms"] == 40
    assert details["tool_name"] == "echo"
    assert details["step_id"] == "step_1"
    assert state.errors[-1].details["cancel_source"] == "deadline"
    assert state.errors[-1].details["step_id"] == "step_1"
