import pytest

from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.schemas.capabilities import CANONICAL_INTENTS, canonical_intent
from multimodal_agent.schemas.planning import IntentResult


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("chat", "direct_chat"),
        ("generate_image", "image_generation"),
        ("understand_image", "image_understanding"),
        ("understand_video", "video_understanding"),
        ("search_product", "product_search"),
        ("compare_price", "price_compare"),
        ("render_3d", "render_3d"),
        ("retrieve_memory", "memory_retrieval"),
        ("multi_tool_task", "multi_step_orchestration"),
        ("ask_followup", "ask_followup"),
    ],
)
def test_legacy_intents_have_canonical_aliases(legacy: str, canonical: str) -> None:
    assert canonical_intent(legacy) == canonical


@pytest.mark.parametrize("intent_name", CANONICAL_INTENTS)
def test_canonical_intents_are_valid_intent_results(intent_name: str) -> None:
    intent = IntentResult(intent=intent_name, confidence=0.9, rationale=f"{intent_name} rationale")

    assert canonical_intent(intent.intent) == intent_name


@pytest.mark.parametrize(
    ("intent_name", "tool_name"),
    [
        ("direct_chat", None),
        ("image_generation", "image_generation"),
        ("image_understanding", "vision_understanding"),
        ("video_understanding", "video_understanding"),
        ("product_search", "product_search"),
        ("price_compare", "price_compare"),
        ("render_3d", "render_3d"),
        ("memory_retrieval", "memory_retrieval"),
    ],
)
def test_router_accepts_canonical_intents(intent_name: str, tool_name: str | None) -> None:
    plan = ToolRouter().route(IntentResult(intent=intent_name, confidence=0.9, rationale="test"))

    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == tool_name


def test_router_accepts_multi_step_orchestration_canonical_intent() -> None:
    plan = ToolRouter().route(
        IntentResult(intent="multi_step_orchestration", confidence=0.9, rationale="test")
    )

    assert [step.tool_name for step in plan.steps] == [
        "video_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]


def test_router_preserves_legacy_alias_behavior() -> None:
    legacy_plan = ToolRouter().route(IntentResult(intent="generate_image", confidence=0.9, rationale="test"))
    canonical_plan = ToolRouter().route(IntentResult(intent="image_generation", confidence=0.9, rationale="test"))

    assert legacy_plan.steps[0].tool_name == canonical_plan.steps[0].tool_name == "image_generation"
