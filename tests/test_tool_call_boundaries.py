import pytest
from pydantic import BaseModel

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.cancellation import AgentRunCancelled
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.realtime_task_state import (
    RealtimeTaskState,
    reduce_realtime_task_state_event,
)
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class BoundaryInput(BaseModel):
    text: str


class BoundarySuccessTool(MockTool):
    name = "boundary_success"
    description = "Boundary success test tool."
    input_schema = BoundaryInput
    output_schema = BoundaryInput

    def _run(self, input: BoundaryInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={
                "summary": f"Echoed {input.text}",
                "raw_provider_payload": {"secret": "must_not_leak"},
            },
            output_ref="mock://boundary/success",
        )


class BoundaryFailureTool(BoundarySuccessTool):
    name = "boundary_failure"
    description = "Boundary failure test tool."

    def _run(self, input: BoundaryInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="provider_token=secret failed",
            data={"raw_provider_payload": {"secret": "must_not_leak"}},
        )


class MutableCancelToken:
    def __init__(
        self, cancelled: bool = False, metadata: dict[str, object] | None = None
    ) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


def test_action_validator_attaches_pre_tool_call_boundary_metadata() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="帮我搜索耳机",
        metadata={
            "realtime_task_state": {
                "schema_version": "realtime_task_state_v1",
                "task_id": "rtask:u1:s1",
                "status": "active",
                "tts_state": "speaking",
            }
        },
    )
    state = AgentState.from_request(request, run_id="run-1")

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="product_search",
            tool_input={"query": "通勤降噪耳机", "idempotency_key": "search-1"},
        ),
        registry=create_default_registry(),
        request=request,
        state=state,
    )

    assert validation.accepted is True
    pre = validation.metadata["pre_tool_call"]
    assert pre["schema_version"] == "tool_call_boundary_v1"
    assert pre["phase"] == "pre_tool_call"
    assert pre["tool_name"] == "product_search"
    assert pre["runtime_identity"] == {
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "run-1",
    }
    assert pre["side_effect"]["level"] == "external_read"
    assert pre["side_effect"]["requires_confirmation"] is False
    assert pre["confirmation"]["required"] is False
    assert pre["idempotency"]["key"] == "search-1"
    assert pre["realtime_task_state"]["task_id"] == "rtask:u1:s1"
    assert pre["input_summary"]["field_count"] == 2
    assert "通勤降噪耳机" not in str(pre)


def test_tool_executor_emits_pre_and_post_tool_call_boundaries_for_success() -> None:
    registry = ToolRegistry()
    registry.register(BoundarySuccessTool())
    sink = ListEventSink()
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="hello"), run_id="run-1"
    )

    result = ToolExecutor(registry=registry, event_sink=sink).run_tool(
        state,
        "step-1",
        "boundary_success",
        {"text": "hello", "idempotency_key": "tool-1"},
    )

    assert result.success is True
    started = next(event for event in sink.events if event.type == "tool_started")
    finished = next(event for event in sink.events if event.type == "tool_finished")
    pre = started.payload["pre_tool_call"]
    post = finished.payload["post_tool_call"]
    assert pre["tool_name"] == "boundary_success"
    assert pre["idempotency"]["key"] == "tool-1"
    assert post["status"] == "succeeded"
    assert post["output_ref"] == "mock://boundary/success"
    assert post["observation_summary"]["summary"] == "Echoed hello"
    assert "raw_provider_payload" not in str(post)


def test_tool_executor_emits_post_tool_call_boundary_for_failure() -> None:
    registry = ToolRegistry()
    registry.register(BoundaryFailureTool())
    sink = ListEventSink()
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="hello"), run_id="run-1"
    )

    result = ToolExecutor(registry=registry, event_sink=sink).run_tool(
        state,
        "step-1",
        "boundary_failure",
        {"text": "hello"},
    )

    assert result.success is False
    failed = next(event for event in sink.events if event.type == "tool_failed")
    post = failed.payload["post_tool_call"]
    assert post["status"] == "failed"
    assert post["tool_name"] == "boundary_failure"
    assert post["observation_summary"]["error_code"]
    assert "secret" not in str(post)


def test_tool_executor_emits_post_tool_call_boundary_for_cancelled_attempt() -> None:
    token = MutableCancelToken(
        metadata={
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
            "deadline_ms": 40,
        }
    )

    class CancellingTool(BoundarySuccessTool):
        name = "boundary_cancel"

        def _run(self, input: BoundaryInput, context: ToolContext) -> ToolResult:
            token.cancelled = True
            return ToolResult(
                tool_name=self.name, success=True, data={"summary": "finished too late"}
            )

    registry = ToolRegistry()
    registry.register(CancellingTool())
    sink = ListEventSink()
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="hello"), run_id="run-1"
    )

    with pytest.raises(AgentRunCancelled):
        ToolExecutor(registry=registry, event_sink=sink, cancel_token=token).run_tool(
            state,
            "step-1",
            "boundary_cancel",
            {"text": "hello"},
        )

    failed = next(event for event in sink.events if event.type == "tool_failed")
    post = failed.payload["post_tool_call"]
    assert post["status"] == "cancelled"
    assert post["cancel"]["source"] == "deadline"
    assert post["cancel"]["deadline_ms"] == 40


def test_realtime_task_state_pending_tool_uses_pre_tool_call_side_effect_summary() -> (
    None
):
    state = RealtimeTaskState(task_id="rtask:u1:s1", user_id="u1", session_id="s1")

    updated = reduce_realtime_task_state_event(
        state,
        event_type="tool.started",
        payload={
            "event_id": "evt-tool-start",
            "tool_name": "image_generation",
            "current_step": "step-1",
            "pre_tool_call": {
                "side_effect": {
                    "level": "compensatable",
                    "requires_confirmation": False,
                    "compensation_hint": "Create a replacement artifact.",
                },
                "confirmation": {"required": False},
            },
        },
    )

    assert updated.pending_tool == {
        "tool_name": "image_generation",
        "status": "working",
        "current_step": "step-1",
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "compensation_hint": "Create a replacement artifact.",
        },
        "requires_confirmation": False,
    }
