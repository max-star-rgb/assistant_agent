import pytest
from pydantic import ValidationError

from assistant_agent.schemas.intent_decision import IntentDecision, PlanStep


def test_valid_direct_chat_intent_decision() -> None:
    decision = IntentDecision(
        primary_intent="direct_chat",
        capabilities=["direct_chat"],
        confidence=0.8,
        source="rule",
        reason="普通对话",
        matched_rules=["fallback_chat"],
    )

    assert decision.primary_intent == "direct_chat"
    assert decision.capabilities == ["direct_chat"]
    assert decision.confidence == 0.8
    assert decision.source == "rule"


def test_capabilities_are_deduped() -> None:
    decision = IntentDecision(
        primary_intent="multi_step_orchestration",
        capabilities=["product_search", "product_search", "price_compare"],
    )

    assert decision.capabilities == ["product_search", "price_compare"]


def test_plan_step_rejects_wrong_tool_for_capability() -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            step_id="step_1",
            capability="image_generation",
            tool_name="price_compare",
        )


def test_plan_step_allows_expected_tool_for_capability() -> None:
    step = PlanStep(
        step_id="step_1",
        capability="image_understanding",
        tool_name="vision_understanding",
        required_inputs=["image"],
    )

    assert step.tool_name == "vision_understanding"
