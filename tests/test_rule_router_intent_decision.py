from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.schemas.requests import UserRequest


def _request(
    text: str,
    image_ids: list[str] | None = None,
    video_ids: list[str] | None = None,
) -> UserRequest:
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text=text,
        image_ids=image_ids or [],
        video_ids=video_ids or [],
    )


def test_rule_router_outputs_intent_decision_for_search_compare() -> None:
    decision = IntentDetector().detect_decision(_request("帮我找白色运动鞋，并比较价格"))

    assert decision.primary_intent == "multi_step_orchestration"
    assert decision.capabilities == ["product_search", "price_compare"]
    assert [step.capability for step in decision.plan_steps] == ["product_search", "price_compare"]
    assert [step.tool_name for step in decision.plan_steps] == ["product_search", "price_compare"]
    assert decision.matched_rules == ["product_search_keywords", "price_compare_keywords"]


def test_rule_router_multi_step_with_media_keeps_ordered_plan_steps() -> None:
    decision = IntentDetector().detect_decision(
        _request("帮我找图里的鞋，比较价格，再生成海报", image_ids=["img1"])
    )

    assert decision.primary_intent == "multi_step_orchestration"
    assert decision.capabilities == [
        "image_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert [step.tool_name for step in decision.plan_steps] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]


def test_rule_router_output_is_validated_before_return() -> None:
    decision = IntentDetector().detect_decision(_request("看看图里有什么"))

    assert decision.primary_intent == "ask_followup"
    assert decision.capabilities == ["ask_followup"]
    assert decision.plan_steps == []
    assert decision.missing_inputs == ["image"]
    assert decision.source == "rule"


def test_legacy_detect_remains_compatible() -> None:
    result = IntentDetector().detect(_request("哪个便宜"))

    assert result.intent == "price_compare"
    assert result.confidence > 0
