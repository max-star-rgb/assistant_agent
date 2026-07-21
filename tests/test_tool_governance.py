"""Focused offline checks for stable tool-governance behavior."""

from datetime import datetime, timezone
from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, model_validator

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    RunToolCatalog,
    ToolResult,
    ToolSpec,
)
from assistant_agent.schemas.tool_spec_adapters import tool_spec_to_openai_tool
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.tool_manifest import (
    PYTHON_INTERPRETER_TOOL_NAME,
    MEMORY_MEDIA_INGEST_TOOL_NAME,
    RENDER_3D_TOOL_NAME,
    WEATHER_TOOL_NAME,
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
    category = "read"
    requires_confirmation = False
    requires_media = ["image"]

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


class _ExecutionBoundaryInput(BaseModel):
    value: str


class _SingleValidationInput(BaseModel):
    value: str
    validation_count: ClassVar[int] = 0

    @model_validator(mode="after")
    def count_validation(self) -> "_SingleValidationInput":
        type(self).validation_count += 1
        return self


class _ExecutionBoundaryTool(MockTool):
    name = "execution_boundary_tool"
    description = "Test-only tool for staged execution."
    input_schema = _ExecutionBoundaryInput
    output_schema = _ExecutionBoundaryInput
    category = "read"
    requires_confirmation = False

    def __init__(self) -> None:
        self.run_count = 0

    def _run(self, input: _ExecutionBoundaryInput, context: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


class _ConfirmationBoundaryTool(_ExecutionBoundaryTool):
    name = "confirmation_boundary_tool"
    category = "write"
    requires_confirmation = True


def test_provider_description_uses_simple_tool_spec() -> None:
    spec = ToolSpec(
        name="canonical_policy_tool",
        description="Read canonical data.",
        category="read",
        requires_confirmation=False,
    )

    payload = tool_spec_to_openai_tool(spec)

    description = payload["function"]["description"]
    assert description == "Read canonical data."


def test_weather_declares_location_and_normalized_target_date() -> None:
    registry = create_default_registry()
    openai_tool = tool_spec_to_openai_tool(registry.get_spec(WEATHER_TOOL_NAME))
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="查一下天气",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=WEATHER_TOOL_NAME,
            tool_input={"location": "   "},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    parameters = openai_tool["function"]["parameters"]
    assert parameters["required"] == ["location"]
    assert {
        "format": "date",
        "type": "string",
    } in parameters["properties"]["target_date"]["anyOf"]
    assert result.accepted is False
    assert result.code == "invalid_tool_input"

    dated_result = ToolExecutor(registry=registry).run_tool(
        AgentState.from_request(request),
        "step-weather",
        WEATHER_TOOL_NAME,
        {
            "location": " 北京 ",
            "target_date": "2026-07-22",
            "days": 2,
        },
    )

    assert dated_result.success is True
    assert [item["date"] for item in dated_result.data["forecast"]] == [
        "2026-07-22",
        "2026-07-23",
    ]


def test_registry_exposes_one_simple_tool_contract() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())

    spec = registry.get_spec(_DeclaredValidationTool.name)

    assert spec.category == "read"
    assert spec.requires_confirmation is False
    assert spec.requires_media == ["image"]
    assert not hasattr(spec, "policy")
    assert not hasattr(spec, "execution")
    assert not hasattr(spec, "when_to_use")
    assert not hasattr(spec, "runtime_constraints")


def test_validated_tool_input_is_reused_by_executor() -> None:
    class SingleValidationTool(_ExecutionBoundaryTool):
        name = "single_validation_tool"
        input_schema = _SingleValidationInput

    _SingleValidationInput.validation_count = 0
    tool = SingleValidationTool()
    registry = ToolRegistry()
    registry.register(tool)
    request = UserRequest(user_id="user-1", session_id="session-1", text="execute")
    state = AgentState.from_request(request)
    decision = AssistantDecision(
        type="tool_call",
        tool_name=tool.name,
        tool_input={"value": "ok"},
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        tool.name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert validation.accepted is True
    assert result.success is True
    assert _SingleValidationInput.validation_count == 1


def test_available_catalog_is_the_execution_boundary() -> None:
    catalog = RunToolCatalog(available_tool_names=["weather"])

    assert catalog.allows("weather") is True
    assert catalog.allows("calendar_create") is False


def test_action_validator_uses_available_catalog_as_its_only_run_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(_ExecutionBoundaryTool())
    request = UserRequest(user_id="user-1", session_id="session-1", text="execute")
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(available_tool_names=[])

    rejected = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_ExecutionBoundaryTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=state,
    )
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[_ExecutionBoundaryTool.name]
    )
    accepted = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_ExecutionBoundaryTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=state,
    )

    assert rejected.code == "tool_not_allowed_for_run"
    assert accepted.code == "accepted"


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


def test_confirmation_is_bound_to_the_declared_tool_name() -> None:
    tool = _ConfirmationBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="write externally",
        metadata={
            "tool_confirmation": {
                "confirmed": True,
                "tool_name": tool.name,
            }
        },
    )

    result = ToolExecutor(registry=registry).run_tool(
        AgentState.from_request(request),
        "step-1",
        tool.name,
        {"value": "write"},
    )

    assert result.success is True
    assert tool.run_count == 1


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
