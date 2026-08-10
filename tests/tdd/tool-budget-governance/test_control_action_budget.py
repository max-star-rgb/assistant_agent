from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.assistant_loop_nodes import (
    AssistantLoopState,
    execute_requested_tool_node,
)
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolBase
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry


class _Input(BaseModel):
    value: str


class _ProbeTool(ToolBase):
    description = "Budget probe."
    input_schema = _Input
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, name: str) -> None:
        self.name = name

    def _run(self, input: _Input, context: object) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


def _execute(
    names: list[str],
    *,
    max_action: int,
    max_control: int,
) -> tuple[AgentState, AssistantLoopState]:
    registry = ToolRegistry()
    for name in sorted(set(names)):
        registry.register(_ProbeTool(name))
    state = AgentState.from_request(UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="budget-sentinel",
    ))
    result = execute_requested_tool_node(cast(AssistantLoopState, {
        "request": state.request,
        "state": state,
        "tool_executor": ToolExecutor(registry=registry),
        "outputs_by_step": {},
        "current_step_index": 0,
        "pending_tool_calls": [
            AssistantToolCall(
                tool_name=name,
                tool_input={"value": f"value-{index}"},
            )
            for index, name in enumerate(names)
        ],
        "tool_observations": [],
        "max_tool_iterations": max_action,
        "max_control_tool_iterations": max_control,
    }))
    return state, result


def test_default_action_and_control_budgets_are_independent() -> None:
    config = ProviderConfig.from_env({})

    assert config.max_tool_iterations == 8
    assert config.max_control_tool_iterations == 3


def test_load_skill_does_not_consume_the_action_tool_budget() -> None:
    state, result = _execute(
        ["load_skill", "business_probe"],
        max_action=1,
        max_control=1,
    )

    assert [call.tool_name for call in state.tool_calls] == [
        "load_skill",
        "business_probe",
    ]
    assert result["tool_calls_used"] == 2
    assert result["control_tool_calls_used"] == 1
    assert result["action_tool_calls_used"] == 1
    assert result["run_phase"].value == "finalize"


def test_control_tool_budget_blocks_additional_skill_loading() -> None:
    state, result = _execute(
        ["load_skill", "load_skill"],
        max_action=1,
        max_control=1,
    )

    assert len(state.tool_calls) == 1
    assert result["control_tool_calls_used"] == 1
    assert result["action_tool_calls_used"] == 0
    assert state.request.metadata["control_tool_calls_skipped_for_budget"] == 1
    assert result["run_phase"].value == "finalize"
