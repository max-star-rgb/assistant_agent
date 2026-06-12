from pydantic import BaseModel

from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.tools.base import MockTool, ToolContext
from multimodal_agent.tools.registry import ToolRegistry


class EchoInput(BaseModel):
    text: str


class EchoTool(MockTool):
    name = "echo"
    description = "Echo input for executor tests."
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text})


def test_tool_executor_updates_state_for_successful_tool_call() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", "echo", {"text": "hello"})

    assert result.success is True
    assert state.tool_calls[0].tool_name == "echo"
    assert state.tool_calls[0].status == "succeeded"
    assert state.tool_results[0].data == {"text": "hello"}
