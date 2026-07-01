from assistant_agent.agent.intent_router_adapter import HybridIntentRouterAdapter, MockLLMIntentRouter
from assistant_agent.schemas.intent_decision import IntentDecision
from assistant_agent.schemas.intent_router import IntentRouterRequest
from assistant_agent.schemas.requests import UserRequest


class StaticRuleRouter:
    def __init__(self, decision: IntentDecision) -> None:
        self.decision = decision
        self.call_count = 0

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        self.call_count += 1
        return self.decision


def _request(text: str) -> IntentRouterRequest:
    return IntentRouterRequest.from_user_request(
        UserRequest(user_id="u1", session_id="s1", text=text)
    )


def test_mock_llm_router_returns_offline_decision() -> None:
    router = MockLLMIntentRouter()

    decision = router.decide(_request("帮我把这个弄得更适合卖"))

    assert decision.source == "mock_llm"
    assert decision.primary_intent == "multi_step_orchestration"
    assert decision.capabilities == ["product_search", "image_generation"]
    assert router.call_count == 1


def test_hybrid_calls_mock_llm_only_on_low_confidence() -> None:
    rule_router = StaticRuleRouter(
        IntentDecision(
            primary_intent="direct_chat",
            capabilities=["direct_chat"],
            confidence=0.45,
            source="rule",
            reason="low confidence rule",
            matched_rules=["fallback_direct_chat"],
        )
    )
    mock_llm = MockLLMIntentRouter()
    hybrid = HybridIntentRouterAdapter(rule_router=rule_router, llm_router=mock_llm)

    decision = hybrid.decide(_request("帮我把这个弄得更适合卖"))

    assert rule_router.call_count == 1
    assert mock_llm.call_count == 1
    assert decision.source == "hybrid"
    assert decision.primary_intent == "multi_step_orchestration"
    assert "fallback_direct_chat" in decision.matched_rules


def test_hybrid_does_not_call_mock_llm_for_high_confidence_rule() -> None:
    rule_router = StaticRuleRouter(
        IntentDecision(
            primary_intent="image_generation",
            capabilities=["image_generation"],
            confidence=0.95,
            source="rule",
            reason="high confidence rule",
            matched_rules=["generate_image_keywords"],
        )
    )
    mock_llm = MockLLMIntentRouter()
    hybrid = HybridIntentRouterAdapter(rule_router=rule_router, llm_router=mock_llm)

    decision = hybrid.decide(_request("生成一张海报"))

    assert rule_router.call_count == 1
    assert mock_llm.call_count == 0
    assert decision.source == "rule"
    assert decision.primary_intent == "image_generation"
