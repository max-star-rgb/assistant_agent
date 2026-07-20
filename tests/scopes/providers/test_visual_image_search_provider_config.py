from assistant_agent.config import ProviderConfig
from assistant_agent.services.tool_visual_image_search_adapter import (
    MockVisualImageSearchAdapter,
    QwenImageSearchAdapter,
    create_visual_image_search_adapter,
)


def test_visual_image_search_provider_defaults_to_mock() -> None:
    config = ProviderConfig.from_env({})
    adapter = create_visual_image_search_adapter(config)

    assert config.visual_image_search_provider == "mock"
    assert config.qwen_image_search_api_key is None
    assert isinstance(adapter, MockVisualImageSearchAdapter)


def test_local_demo_profile_does_not_select_qwen_visual_image_search_from_keys() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER": "qwen",
            "QWEN_IMAGE_SEARCH_API_KEY": "sk-qwen-image-search-test",
            "QWEN_IMAGE_SEARCH_BASE_URL": "https://dashscope.local/compatible-mode/v1",
            "QWEN_IMAGE_SEARCH_MODEL": "qwen3.7-plus",
        }
    )

    assert config.runtime_profile.name == "local_demo"
    assert config.visual_image_search_provider == "mock"
    assert isinstance(create_visual_image_search_adapter(config), MockVisualImageSearchAdapter)


def test_provider_smoke_explicitly_selects_qwen_visual_image_search() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER": "qwen",
            "DASHSCOPE_API_KEY": "sk-dashscope-image-search-test",
            "QWEN_IMAGE_SEARCH_BASE_URL": "https://dashscope.local/compatible-mode/v1",
            "QWEN_IMAGE_SEARCH_MODEL": "qwen-image-search-test",
            "QWEN_IMAGE_SEARCH_TIMEOUT_SECONDS": "8.5",
        }
    )
    adapter = create_visual_image_search_adapter(config)

    assert config.visual_image_search_provider == "qwen"
    assert config.qwen_image_search_api_key == "sk-dashscope-image-search-test"
    assert config.qwen_image_search_base_url == "https://dashscope.local/compatible-mode/v1"
    assert config.qwen_image_search_model == "qwen-image-search-test"
    assert config.qwen_image_search_timeout_seconds == 8.5
    assert isinstance(adapter, QwenImageSearchAdapter)
    assert adapter.config.api_key == "sk-dashscope-image-search-test"


def test_visual_image_search_key_counts_as_real_provider_config() -> None:
    config = ProviderConfig.from_env({"QWEN_IMAGE_SEARCH_API_KEY": "sk-qwen-image-search-test"})

    assert config.has_any_real_provider() is True
