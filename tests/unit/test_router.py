import pytest

from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.schemas.planning import IntentResult


def intent(name: str) -> IntentResult:
    return IntentResult(intent=name, confidence=0.9, rationale=f"{name} rationale")


@pytest.mark.parametrize(
    ("intent_name", "tool_name"),
    [
        ("understand_image", "vision_understanding"),
        ("understand_video", "vision_understanding"),
        ("search_product", "product_search"),
        ("compare_price", "price_compare"),
        ("generate_image", "image_generation"),
        ("render_3d", "render_3d"),
        ("retrieve_memory", "memory_retrieval"),
        ("save_memory", "memory_save"),
    ],
)
def test_routes_single_tool_intents(intent_name: str, tool_name: str) -> None:
    plan = ToolRouter().route(intent(intent_name))

    assert plan.requires_followup is False
    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == tool_name


def test_routes_chat_without_tool() -> None:
    plan = ToolRouter().route(intent("chat"))

    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name is None
    assert plan.steps[0].action == "chat"


def test_routes_ask_followup_without_tool_steps() -> None:
    plan = ToolRouter().route(
        IntentResult(
            intent="ask_followup",
            confidence=0.8,
            missing_slots=["context"],
            rationale="缺少上下文",
        )
    )

    assert plan.requires_followup is True
    assert plan.steps == []
    assert plan.followup_question is not None


def test_routes_multi_tool_task_in_required_order() -> None:
    plan = ToolRouter().route(intent("multi_tool_task"))

    assert [step.tool_name for step in plan.steps] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert plan.steps[1].depends_on == ["step_1"]
    assert plan.steps[2].depends_on == ["step_2"]
    assert plan.steps[3].depends_on == ["step_3"]


def test_select_tools_returns_tool_selections_for_plan_steps() -> None:
    selections = ToolRouter().select_tools(intent("multi_tool_task"))

    assert [selection.tool_name for selection in selections] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert selections[0].step_id == "step_1"
