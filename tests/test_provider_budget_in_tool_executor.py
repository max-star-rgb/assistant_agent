from pydantic import BaseModel

from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.schemas.planning import TaskStep
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.provider_budget import ProviderCallBudget
from multimodal_agent.tools.base import MockTool, ToolContext
from multimodal_agent.tools.registry import ToolRegistry


class BudgetInput(BaseModel):
    text: str


class CountingTool(MockTool):
    name = "counting_tool"
    description = "Counts executions."
    input_schema = BudgetInput
    output_schema = BudgetInput

    def __init__(self) -> None:
        self.calls = 0

    def _run(self, input: BudgetInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"text": input.text, "provider": "mock", "model": "mock-model", "estimated_cost": 0.01},
        )


def test_tool_executor_records_provider_call_budget() -> None:
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        tool.name,
        {"text": "hello"},
        step=TaskStep(step_id="step_1", action="generate_image", tool_name=tool.name),
    )

    assert result.success is True
    assert tool.calls == 1
    assert state.provider_budget.provider_call_count == 1
    assert state.provider_budget.capability_call_count("image_generation") == 1
    assert state.provider_budget.call_records[0].provider == "mock"
    assert state.provider_budget.call_records[0].estimated_cost == 0.01


def test_tool_executor_blocks_before_call_when_budget_exceeded() -> None:
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    state.provider_budget = ProviderCallBudget(max_provider_calls_per_run=0)

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        tool.name,
        {"text": "hello"},
        step=TaskStep(step_id="step_1", action="generate_image", tool_name=tool.name),
    )

    assert result.success is False
    assert tool.calls == 0
    assert result.contract is not None
    assert result.contract.status == "failed"
    assert result.contract.errors[0].code == "provider_call_limit_exceeded"
    assert state.errors[0].details["code"] == "provider_call_limit_exceeded"


def test_tool_executor_blocks_per_capability_limit() -> None:
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    state.provider_budget = ProviderCallBudget(max_calls_per_capability={"image_generation": 0})

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        tool.name,
        {"text": "hello"},
        step=TaskStep(step_id="step_1", action="generate_image", tool_name=tool.name),
    )

    assert result.success is False
    assert tool.calls == 0
    assert state.errors[0].details["provider_budget"]["provider_call_count"] == 0
