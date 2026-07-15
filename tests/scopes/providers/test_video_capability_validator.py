from assistant_agent.agent.capability_validator import CapabilityValidator
from assistant_agent.schemas.intent_decision import IntentDecision
from assistant_agent.schemas.requests import UserRequest


def test_video_present_but_direct_chat_stays_direct_chat() -> None:
    decision = IntentDecision(primary_intent="direct_chat", confidence=0.8)

    validated = CapabilityValidator().validate(
        decision,
        UserRequest(user_id="u1", session_id="s1", text="写一段商品介绍", video_ids=["video1"]),
    )

    assert validated.primary_intent == "direct_chat"
    assert validated.capabilities == ["direct_chat"]
    assert validated.plan_steps[0].tool_name is None
