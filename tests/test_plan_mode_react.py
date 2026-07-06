from pydantic import BaseModel

from assistant_agent.agent.plan_validator import PlanValidator
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class NativeFinalChatAdapter:
    provider = "scripted-native"
    model = "native-test"

    def __init__(self, message: str = "native done") -> None:
        self.message = message
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            response_text=self.message,
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            message_kind="final_answer",
        )


class EchoInput(BaseModel):
    text: str


class EchoTool(MockTool):
    name = "echo"
    description = "Echo text for plan validator tests."
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text}, output_ref=f"echo://{input.text}")


def test_default_execution_strategy_remains_react_and_uses_native_runtime() -> None:
    adapter = NativeFinalChatAdapter("ok")
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert state.execution_strategy == "react"
    assert state.plan is None
    assert state.response is not None
    assert state.response.message == "ok"
    assert len(adapter.requests) == 1
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"


def test_plan_and_solve_request_is_accepted_but_no_llm_json_plan_controller_runs() -> None:
    adapter = NativeFinalChatAdapter("native plan strategy response")
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="legacy strategy", execution_strategy="plan_and_solve")
    )

    assert state.execution_strategy == "plan_and_solve"
    assert state.plan is None
    assert state.response is not None
    assert state.response.message == "native plan strategy response"
    assert len(adapter.requests) == 1
    assert adapter.requests[0].tools
    assert "plan-and-solve controller" not in adapter.requests[0].user_query


def test_plan_validator_rejects_cycles_and_step_limits() -> None:
    registry = _registry(EchoTool())
    cycle = TaskPlan(
        goal="cycle",
        steps=[
            TaskStep(step_id="step_1", action="a", tool_name="echo", depends_on=["step_2"]),
            TaskStep(step_id="step_2", action="b", tool_name="echo", depends_on=["step_1"]),
        ],
    )
    too_large = TaskPlan(
        goal="large",
        steps=[
            TaskStep(step_id=f"step_{index}", action="a", tool_name="echo")
            for index in range(1, 4)
        ],
    )

    assert PlanValidator().validate(cycle, registry).code == "cyclic_dependency"
    assert PlanValidator(max_steps=2).validate(too_large, registry).code == "plan_too_large"


def _registry(*tools: MockTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry
