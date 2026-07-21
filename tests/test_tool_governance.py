"""Focused offline checks for stable tool-governance behavior."""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, Field

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ToolPolicyMetadata,
    ToolResult,
    VisibilityPolicy,
)
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
