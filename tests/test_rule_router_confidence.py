from assistant_agent.agent.intent import IntentDetector
from assistant_agent.schemas.requests import UserRequest


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


def test_rule_router_high_confidence_image_generation() -> None:
    decision = IntentDetector().detect_decision(_request("生成一张日系极简海报"))

    assert decision.primary_intent == "image_generation"
    assert decision.confidence >= 0.85
    assert decision.source == "rule"
    assert decision.matched_rules == ["generate_image_keywords"]
    assert decision.reason


def test_rule_router_high_confidence_direct_chat() -> None:
    decision = IntentDetector().detect_decision(_request("这个风格怎么样"))

    assert decision.primary_intent == "direct_chat"
    assert decision.confidence >= 0.55
    assert decision.source == "rule"
    assert decision.matched_rules == ["fallback_direct_chat"]


def test_rule_router_ambiguous_low_confidence_followup() -> None:
    decision = IntentDetector().detect_decision(_request("这个"))

    assert decision.primary_intent == "ask_followup"
    assert decision.confidence <= 0.55
    assert decision.matched_rules == ["vague_reference"]
    assert decision.missing_inputs == ["context"]
