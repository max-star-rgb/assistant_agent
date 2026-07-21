"""Focused offline checks for stable tool-governance behavior."""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, Field

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ToolPolicyMetadata,
    ToolExecutionPolicy,
    ToolResult,
    ToolSpec,
    VisibilityPolicy,
)
from assistant_agent.schemas.tool_spec_adapters import tool_spec_to_openai_tool
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.tool_manifest import (
    PYTHON_INTERPRETER_TOOL_NAME,
    MEMORY_MEDIA_INGEST_TOOL_NAME,
    RENDER_3D_TOOL_NAME,
)
from assistant_agent.tools.base import MockTool, ToolContext, ToolInputValidationError
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class _DeclaredValidationInput(BaseModel):
    value: str = Field(min_length=1)


class _DeclaredValidationTool(MockTool):
    name = "declared_validation_tool"
    description = "Test-only tool with declarative media and tool-owned validation."
    input_schema = _DeclaredValidationInput
    output_schema = _DeclaredValidationInput
    policy = ToolPolicyMetadata(
        risk="pure",
        approval=ApprovalPolicy(mode="never"),
        visibility=VisibilityPolicy(requires_media=["image"]),
    )

    def validate_call(self, input: _DeclaredValidationInput) -> None:
        if input.value == "blocked":
            raise ToolInputValidationError(
                "tool_owned_validation_failed",
                "The tool-owned validator rejected this input.",
            )

    def _run(
        self, input: _DeclaredValidationInput, context: ToolContext
    ) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


class _ConflictingWeatherPolicyTool(_DeclaredValidationTool):
    name = "weather"
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="always"),
    )


class _ExecutionBoundaryInput(BaseModel):
    value: str


class _ExecutionBoundaryTool(MockTool):
    name = "execution_boundary_tool"
    description = "Test-only tool for staged execution."
    input_schema = _ExecutionBoundaryInput
    output_schema = _ExecutionBoundaryInput
    policy = ToolPolicyMetadata(
        risk="pure",
        approval=ApprovalPolicy(mode="never"),
    )

    def __init__(self) -> None:
        self.run_count = 0

    def _run(self, input: _ExecutionBoundaryInput, context: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


class _ConfirmationBoundaryTool(_ExecutionBoundaryTool):
    name = "confirmation_boundary_tool"
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="always"),
    )


def test_provider_description_uses_canonical_policy_view() -> None:
    spec = ToolSpec(
        name="canonical_policy_tool",
        policy=ToolPolicyMetadata(
            risk="pure",
            approval=ApprovalPolicy(mode="never"),
        ),
    )

    payload = tool_spec_to_openai_tool(spec)

    description = payload["function"]["description"]
    assert "level=none" in description
    assert "requires_confirmation=false" in description
    assert "level=pending_confirmation" not in description


def test_registry_derives_side_effect_from_declarative_policy() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())

    spec = registry.get_spec(_DeclaredValidationTool.name)

    assert spec.side_effect.level == "none"
    assert spec.side_effect.requires_confirmation is False


def test_parallel_safe_defaults_to_false() -> None:
    assert ToolExecutionPolicy().parallel_safe is False


def test_tool_executor_stages_do_not_commit_during_invocation() -> None:
    tool = _ExecutionBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    events = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=events)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="execute",
    )
    state = AgentState.from_request(request)

    prepared = executor.prepare_tool_call(
        state,
        "step-1",
        tool.name,
        {"value": "ok"},
    )

    assert tool.run_count == 0
    assert len(state.tool_calls) == 1
    assert state.tool_results == []
    assert [event.type for event in events.events] == ["tool_started"]

    invocation = executor.invoke_tool(prepared)

    assert tool.run_count == 1
    assert state.tool_results == []
    assert [event.type for event in events.events] == ["tool_started"]

    result = executor.commit_tool_result(state, prepared, invocation)

    assert result.success is True
    assert [item.data for item in state.tool_results] == [{"value": "ok"}]
    assert [event.type for event in events.events] == ["tool_started", "tool_finished"]


def test_prepare_reserves_budget_before_a_future_parallel_batch_invokes() -> None:
    tool = _ExecutionBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry=registry)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="execute twice",
    )
    state = AgentState.from_request(request)
    state.provider_budget.max_provider_calls_per_run = 1

    first = executor.prepare_tool_call(state, "step-1", tool.name, {"value": "first"})
    second = executor.prepare_tool_call(state, "step-2", tool.name, {"value": "second"})

    assert first.budget_reservation is not None
    assert first.budget_error is None
    assert second.budget_reservation is None
    assert second.budget_error is not None
    assert second.budget_error.code == "provider_call_limit_exceeded"

    first_result = executor.commit_tool_result(state, first, executor.invoke_tool(first))
    second_result = executor.commit_tool_result(state, second, executor.invoke_tool(second))

    assert first_result.success is True
    assert second_result.success is False
    assert tool.run_count == 1
    assert state.provider_budget.provider_call_count == 1
    assert state.provider_budget.pending_reservations == []


def test_staged_executor_preserves_confirmation_without_invoking_tool() -> None:
    tool = _ConfirmationBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    events = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=events)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="write externally",
    )
    state = AgentState.from_request(request)

    result = executor.run_tool(state, "step-1", tool.name, {"value": "write"})

    assert result.success is True
    assert result.data["status"] == "confirmation_required"
    assert tool.run_count == 0
    assert state.provider_budget.provider_call_count == 0
    assert state.provider_budget.pending_reservations == []
    assert [event.type for event in events.events] == ["tool_started", "tool_finished"]


def test_registry_rejects_conflicting_side_effect_declarations() -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="Conflicting side-effect declarations"):
        registry.register(_ConflictingWeatherPolicyTool())


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        (RENDER_3D_TOOL_NAME, {"scene_description": "客厅"}),
        (
            MEMORY_MEDIA_INGEST_TOOL_NAME,
            {
                "files": [
                    {
                        "file_url": "local://media/example.mp4",
                        "filename": "example.mp4",
                        "media_type": "video",
                        "start_time": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        ),
    ],
)
def test_llm_selected_tool_is_not_rejected_by_natural_language_intent_rules(
    tool_name: str,
    tool_input: dict[str, object],
) -> None:
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=tool_name,
            tool_input=tool_input,
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is True
    assert result.code == "accepted"


def test_declared_media_requirement_is_enforced_without_tool_name_branch() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_DeclaredValidationTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "missing_required_input"


def test_tool_owned_validator_runs_without_action_validator_branch() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
        image_ids=["image-1"],
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_DeclaredValidationTool.name,
            tool_input={"value": "blocked"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "tool_owned_validation_failed"


def test_python_safety_validation_is_owned_by_python_tool() -> None:
    registry = create_default_registry()
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="分析代码",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=PYTHON_INTERPRETER_TOOL_NAME,
            tool_input={"code": 'open("secret.txt")'},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "unsafe_tool_input"
