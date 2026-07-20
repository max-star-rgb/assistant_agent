from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSelection
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.memory_tool import MemorySaveTool
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool
from assistant_agent.tools.registry import ToolRegistry


class StaticIntentDetector:
    def detect(self, request: UserRequest) -> IntentResult:
        return IntentResult(intent="multi_tool_task", confidence=1.0, rationale="test")


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


class FailingShoppingSearchTool(ShoppingSearchTool):
    def _run(self, input, context) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error="provider_bad_response: malformed payload")


class TimeoutShoppingSearchTool(ShoppingSearchTool):
    def _run(self, input, context) -> ToolResult:
        raise TimeoutError("provider timed out after 10s secret=abc123")


def test_provider_unconfigured_records_structured_error() -> None:
    state = AgentGraphRuntime(
        config=ProviderConfig(vision_provider="openai", openai_api_key=None),
    ).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="图里是什么",
            image_ids=["img1"],
        )
    )

    assert state.status == "failed"
    assert state.response is not None
    assert state.errors[0].details["code"] == "provider_unconfigured"
    assert state.response.data["errors"][0]["code"] == "provider_unconfigured"
    assert "处理失败" in state.response.message


def test_tool_timeout_is_structured_and_sanitized() -> None:
    state = _runtime_with_plan(
        TaskPlan(
            goal="timeout",
            steps=[TaskStep(step_id="step_1", action="shopping_search", tool_name="shopping_search")],
        ),
        TimeoutShoppingSearchTool(),
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="找相似款"))

    assert state.status == "failed"
    assert state.errors[0].details["code"] == "provider_timeout"
    assert "secret" not in state.errors[0].message.lower()
    assert "[redacted]" in state.errors[0].message


def test_optional_step_failed_but_graph_continues() -> None:
    state = _runtime_with_plan(
        TaskPlan(
            goal="optional failure",
            steps=[
                TaskStep(
                    step_id="step_1",
                    action="shopping_search",
                    tool_name="shopping_search",
                    optional=True,
                ),
                TaskStep(step_id="step_2", action="generate_image", tool_name="image_generation"),
            ],
        ),
        FailingShoppingSearchTool(),
    ).run_state(_generate_image_request())

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls[:2]] == ["shopping_search", "image_generation"]
    assert state.run_tool_set is not None
    assert state.run_tool_set.qualified_tool_names == state.run_tool_set.exposed_tool_names
    assert state.run_tool_set.exposed_tool_names == state.run_tool_set.executable_tool_names
    assert state.run_tool_set.selection_reasons == ["recall_identity"]
    assert state.errors[0].details["recovery_action"] == "continue_with_partial_result"
    assert state.response is not None
    assert state.response.data["partial_success"] is True


def test_required_step_failed_then_graph_stops() -> None:
    state = _runtime_with_plan(
        TaskPlan(
            goal="required failure",
            steps=[
                TaskStep(step_id="step_1", action="shopping_search", tool_name="shopping_search"),
                TaskStep(step_id="step_2", action="generate_image", tool_name="image_generation"),
            ],
        ),
        FailingShoppingSearchTool(),
    ).run_state(_generate_image_request())

    assert state.status == "failed"
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert state.errors[0].details["recovery_action"] == "stop_with_error"
    assert state.response is not None
    assert state.response.data["partial_success"] is False


def test_final_response_contains_failure_explanation() -> None:
    state = _runtime_with_plan(
        TaskPlan(
            goal="partial response",
            steps=[
                TaskStep(
                    step_id="step_1",
                    action="shopping_search",
                    tool_name="shopping_search",
                    optional=True,
                ),
                TaskStep(step_id="step_2", action="generate_image", tool_name="image_generation"),
            ],
        ),
        FailingShoppingSearchTool(),
    ).run_state(_generate_image_request())

    assert state.response is not None
    assert "部分步骤失败" in state.response.message
    assert "provider_bad_response" in state.response.message


def _runtime_with_plan(plan: TaskPlan, shopping_tool: ShoppingSearchTool) -> AgentGraphRuntime:
    registry = ToolRegistry()
    registry.register(shopping_tool)
    registry.register(ImageGenerationTool())
    registry.register(MemorySaveTool())
    return AgentGraphRuntime(
        registry=registry,
        intent_detector=StaticIntentDetector(),
        router=StaticRouter(plan),
    )


def _generate_image_request() -> UserRequest:
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text="生成一张海报",
        metadata={"tool_visibility": {"enabled_tools": ["image_generation"]}},
    )
