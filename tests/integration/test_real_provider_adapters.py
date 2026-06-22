import pytest

from multimodal_agent.config import ProviderConfig, should_run_integration_tests
from multimodal_agent.services.provider_selection import create_vision_adapter
from multimodal_agent.services.real_vision_adapter import HttpVisionProviderAdapter
from multimodal_agent.services.vision_adapter import VisionUnderstandingInput


def configured_real_vision_provider() -> ProviderConfig:
    config = ProviderConfig.from_env()
    if not should_run_integration_tests():
        pytest.skip("set RUN_INTEGRATION_TESTS=1 to run real provider integration tests")
    if config.vision_provider == "mock":
        pytest.skip("set MULTIMODAL_AGENT_VISION_PROVIDER=openai|qwen")
    if config.vision_provider == "openai" and not config.openai_api_key:
        pytest.skip("set OPENAI_API_KEY to run OpenAI vision integration test")
    if config.vision_provider == "qwen" and not config.qwen_vision_api_key:
        pytest.skip("set QWEN_VISION_API_KEY to run Qwen vision integration test")
    return config


def test_real_vision_provider_selection_is_env_gated() -> None:
    config = configured_real_vision_provider()

    adapter = create_vision_adapter(config)

    assert isinstance(adapter, HttpVisionProviderAdapter)


def test_real_vision_provider_smoke_call_is_env_gated() -> None:
    config = configured_real_vision_provider()

    result = create_vision_adapter(config).understand(
        VisionUnderstandingInput(
            image_ids=["integration-image-placeholder"],
            question="图里是什么",
        )
    )

    assert result.summary
