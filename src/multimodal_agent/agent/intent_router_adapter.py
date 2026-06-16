"""Optional intent router adapters for Phase 5F."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from multimodal_agent.agent.capability_validator import CapabilityValidator
from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.intent_decision import IntentDecision
from multimodal_agent.schemas.intent_router import IntentRouterRequest


class IntentRouterAdapter(Protocol):
    """Common interface for rule, mock LLM, hybrid, and optional LLM routers."""

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        """Return a validated intent decision."""


class RuleIntentRouterAdapter:
    """Adapter wrapper around the deterministic rule router."""

    def __init__(self, detector: IntentDetector | None = None) -> None:
        self.detector = detector or IntentDetector()

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        return self.detector.detect_decision(request.request)


class MockLLMIntentRouter:
    """Offline mock LLM router for tests and hybrid fallback development."""

    def __init__(
        self,
        outputs: Mapping[str, Mapping[str, Any]] | None = None,
        validator: CapabilityValidator | None = None,
    ) -> None:
        self.outputs = dict(outputs or {})
        self.validator = validator or CapabilityValidator()
        self.call_count = 0

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        self.call_count += 1
        raw_output = self.outputs.get(request.user_query or "", self._default_output(request))
        try:
            decision = IntentDecision.model_validate(raw_output)
        except ValidationError as exc:
            return IntentDecision(
                primary_intent="ask_followup",
                capabilities=["ask_followup"],
                missing_inputs=["intent_decision"],
                confidence=0.0,
                source="fallback",
                reason=f"mock_llm_intent_router_malformed_output: {exc.errors()[0]['type']}",
            )
        validated = self.validator.validate(decision, request.request)
        return validated.model_copy(update={"source": "mock_llm" if validated.source != "fallback" else "fallback"})

    def _default_output(self, request: IntentRouterRequest) -> dict[str, Any]:
        query = request.user_query or ""
        if "卖" in query or "营销" in query:
            return {
                "primary_intent": "multi_step_orchestration",
                "capabilities": ["product_search", "image_generation"],
                "confidence": 0.72,
                "source": "mock_llm",
                "reason": "mock LLM 判断用户需要商品信息和生成营销素材。",
                "matched_rules": ["mock_llm_sales_improvement"],
            }
        return {
            "primary_intent": "ask_followup",
            "capabilities": ["ask_followup"],
            "missing_inputs": ["clarification"],
            "confidence": 0.45,
            "source": "mock_llm",
            "reason": "mock LLM 无法可靠判断用户意图，需要追问。",
            "matched_rules": ["mock_llm_low_confidence"],
        }


class HybridIntentRouterAdapter:
    """Use rule router first, then mock LLM only for low-confidence decisions."""

    def __init__(
        self,
        rule_router: IntentRouterAdapter | None = None,
        llm_router: IntentRouterAdapter | None = None,
        low_confidence_threshold: float = 0.55,
    ) -> None:
        self.rule_router = rule_router or RuleIntentRouterAdapter()
        self.llm_router = llm_router or MockLLMIntentRouter()
        self.low_confidence_threshold = low_confidence_threshold

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        rule_decision = self.rule_router.decide(request)
        if rule_decision.confidence <= self.low_confidence_threshold:
            llm_decision = self.llm_router.decide(request)
            return llm_decision.model_copy(
                update={
                    "source": "hybrid",
                    "matched_rules": [*rule_decision.matched_rules, *llm_decision.matched_rules],
                    "reason": f"{rule_decision.reason}；hybrid mock_llm fallback：{llm_decision.reason}",
                }
            )
        return rule_decision


class OpenAICompatibleIntentRouter:
    """Skeleton for future real LLM routing. It never calls a network provider."""

    def __init__(self, validator: CapabilityValidator | None = None) -> None:
        self.validator = validator or CapabilityValidator()

    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        decision = IntentDecision(
            primary_intent="ask_followup",
            capabilities=["ask_followup"],
            missing_inputs=["intent_router_provider"],
            confidence=0.0,
            source="fallback",
            reason="llm_intent_router_not_configured: real LLM routing is default-off.",
        )
        return self.validator.validate(decision, request.request)


def create_intent_router_adapter(config: ProviderConfig | None = None) -> IntentRouterAdapter:
    """Create an intent router adapter from config without enabling real LLM by default."""

    resolved = config or ProviderConfig()
    if resolved.intent_router == "mock_llm":
        return MockLLMIntentRouter()
    if resolved.intent_router == "hybrid":
        return HybridIntentRouterAdapter()
    if resolved.intent_router == "llm":
        return OpenAICompatibleIntentRouter()
    return RuleIntentRouterAdapter()
