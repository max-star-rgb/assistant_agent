from multimodal_agent.agent.intent_router_adapter import (
    MockLLMIntentRouter,
    OpenAICompatibleIntentRouter,
)
from multimodal_agent.schemas.intent_router import IntentRouterRequest
from multimodal_agent.schemas.requests import UserRequest


def _router_request(text: str, image_ids: list[str] | None = None) -> IntentRouterRequest:
    return IntentRouterRequest.from_user_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text=text,
            image_ids=image_ids or [],
        )
    )


def test_mock_llm_output_is_parsed_as_intent_decision_and_validated() -> None:
    router = MockLLMIntentRouter(
        outputs={
            "看看图里有什么": {
                "primary_intent": "image_understanding",
                "capabilities": ["image_understanding"],
                "confidence": 0.8,
                "source": "mock_llm",
                "reason": "mock output",
            }
        }
    )

    decision = router.decide(_router_request("看看图里有什么"))

    assert decision.primary_intent == "ask_followup"
    assert decision.source == "mock_llm"
    assert decision.missing_inputs == ["image"]


def test_malformed_mock_output_returns_structured_fallback() -> None:
    router = MockLLMIntentRouter(outputs={"bad": {"primary_intent": "unknown"}})

    decision = router.decide(_router_request("bad"))

    assert decision.primary_intent == "ask_followup"
    assert decision.source == "fallback"
    assert decision.missing_inputs == ["intent_decision"]
    assert "malformed_output" in decision.reason


def test_openai_compatible_skeleton_does_not_call_real_llm() -> None:
    decision = OpenAICompatibleIntentRouter().decide(_router_request("帮我判断意图"))

    assert decision.primary_intent == "ask_followup"
    assert decision.source == "fallback"
    assert decision.missing_inputs == ["intent_router_provider"]
    assert "default-off" in decision.reason
