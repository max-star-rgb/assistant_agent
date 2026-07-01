from assistant_agent.agent.intent_router_adapter import (
    HybridIntentRouterAdapter,
    MockLLMIntentRouter,
    OpenAICompatibleIntentRouter,
    RuleIntentRouterAdapter,
    create_intent_router_adapter,
)
from assistant_agent.config import ProviderConfig


def test_default_intent_router_is_rule() -> None:
    config = ProviderConfig.from_env({})
    adapter = create_intent_router_adapter(config)

    assert config.intent_router == "rule"
    assert isinstance(adapter, RuleIntentRouterAdapter)


def test_config_reads_mock_llm_intent_router() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_INTENT_ROUTER": "mock_llm"})
    adapter = create_intent_router_adapter(config)

    assert config.intent_router == "mock_llm"
    assert isinstance(adapter, MockLLMIntentRouter)


def test_config_reads_hybrid_intent_router() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_INTENT_ROUTER": "hybrid"})
    adapter = create_intent_router_adapter(config)

    assert config.intent_router == "hybrid"
    assert isinstance(adapter, HybridIntentRouterAdapter)


def test_llm_intent_router_selects_default_off_skeleton() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_INTENT_ROUTER": "llm"})
    adapter = create_intent_router_adapter(config)

    assert config.intent_router == "llm"
    assert isinstance(adapter, OpenAICompatibleIntentRouter)


def test_unknown_intent_router_falls_back_to_rule() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_INTENT_ROUTER": "real-provider"})
    adapter = create_intent_router_adapter(config)

    assert config.intent_router == "rule"
    assert isinstance(adapter, RuleIntentRouterAdapter)
