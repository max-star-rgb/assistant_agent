from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSelection
from assistant_agent.services.provider_policy import ProviderExecutionPolicy, RetryPolicy
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


class StaticIntentDetector:
    def detect(self, request: UserRequest) -> IntentResult:
        return IntentResult(intent="multi_step_orchestration", confidence=1.0, rationale="test")


class StaticRouter:
    def __init__(self, plan: TaskPlan) -> None:
        self.plan = plan

    def route(self, intent: IntentResult) -> TaskPlan:
        return self.plan

    def select_tools(self, intent: IntentResult) -> list[ToolSelection]:
        return [
            ToolSelection(tool_name=step.tool_name, reason="test", step_id=step.step_id)
            for step in self.plan.steps
            if step.tool_name is not None
        ]


class OptionalTimeoutShoppingSearchTool(ShoppingSearchTool):
    def _run(self, input, context) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error="provider_timeout: price source timed out")


def test_partial_result_response_summarizes_optional_provider_failure() -> None:
    registry = ToolRegistry()
    registry.register(OptionalTimeoutShoppingSearchTool())
    registry.register(ImageGenerationTool())
    runtime = AgentGraphRuntime(
        registry=registry,
        intent_detector=StaticIntentDetector(),
        router=StaticRouter(
            TaskPlan(
                goal="partial",
                steps=[
                    TaskStep(
                        step_id="step_1",
                        action="shopping_search",
                        tool_name="shopping_search",
                        optional=True,
                    ),
                    TaskStep(step_id="step_2", action="generate_image", tool_name="image_generation"),
                ],
            )
        ),
    )
    runtime.tool_executor.execution_policy = ProviderExecutionPolicy(retry=RetryPolicy(max_retries=0))

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报"))

    assert state.status == "completed"
    assert state.response is not None
    assert state.response.data["partial_success"] is True
    assert "部分步骤失败" in state.response.message
    assert "provider_timeout" in state.response.message
    assert state.errors[0].details["recovery_action"] == "continue_with_partial_result"
