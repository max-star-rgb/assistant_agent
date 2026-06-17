from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.chat_adapter import UnconfiguredChatAdapter, create_chat_adapter
from multimodal_agent.services.image_generation_adapter import (
    ImageGenerationInput,
    UnconfiguredImageGenerationAdapter,
    create_image_generation_adapter,
)
from multimodal_agent.services.provider_selection import create_vision_adapter
from multimodal_agent.services.real_vision_adapter import HttpVisionProviderAdapter
from multimodal_agent.services.vision_adapter import MockVisionUnderstandingAdapter


def test_offline_eval_blocks_real_provider_selectors_from_environment() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "offline_eval",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "openai",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "comfyui",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "http",
            "MULTIMODAL_AGENT_PRICE_PROVIDER": "http",
            "MULTIMODAL_AGENT_RENDER_PROVIDER": "http",
            "MULTIMODAL_AGENT_VIDEO_PROVIDER": "http",
            "QWEN_API_KEY": "sk-runtime-profile-test",
            "OPENAI_API_KEY": "sk-runtime-profile-test",
            "COMFYUI_BASE_URL": "http://provider.local",
            "PRODUCT_SEARCH_BASE_URL": "http://provider.local",
            "PRODUCT_SEARCH_API_KEY": "sk-runtime-profile-test",
            "PRICE_COMPARE_BASE_URL": "http://provider.local",
            "PRICE_COMPARE_API_KEY": "sk-runtime-profile-test",
            "RENDER_BASE_URL": "http://provider.local",
            "RENDER_API_KEY": "sk-runtime-profile-test",
            "VIDEO_UNDERSTANDING_BASE_URL": "http://provider.local",
            "VIDEO_UNDERSTANDING_API_KEY": "sk-runtime-profile-test",
        }
    )

    assert config.runtime_profile.name == "offline_eval"
    assert config.vision_provider == "mock"
    assert config.chat_provider == "mock"
    assert config.image_generation_provider == "mock"
    assert config.product_search_provider == "mock"
    assert config.price_compare_provider == "mock"
    assert config.render_provider == "mock"
    assert config.video_provider == "mock"
    assert isinstance(create_vision_adapter(config), MockVisionUnderstandingAdapter)


def test_provider_smoke_missing_real_provider_config_does_not_fallback_to_mock() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "openai",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai",
        }
    )

    vision_adapter = create_vision_adapter(config)
    chat_adapter = create_chat_adapter(config)
    image_adapter = create_image_generation_adapter(config)

    assert isinstance(vision_adapter, HttpVisionProviderAdapter)
    assert isinstance(chat_adapter, UnconfiguredChatAdapter)
    assert isinstance(image_adapter, UnconfiguredImageGenerationAdapter)

    image_result = image_adapter.generate(ImageGenerationInput(prompt="生成一张海报"))
    assert image_result.status == "failed"
    assert image_result.provider == "openai"
    assert image_result.errors[0]["code"] == "provider_unconfigured"


def test_default_runtime_entry_remains_offline_even_with_provider_keys() -> None:
    config = ProviderConfig.from_env({"OPENAI_API_KEY": "sk-runtime-profile-test"})
    runtime = AgentGraphRuntime(config=config)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我写一段商品介绍",
        )
    )

    assert config.runtime_profile.name == "local_demo"
    assert config.chat_provider == "mock"
    assert state.status == "completed"
    assert state.response is not None
    assert "离线 mock direct_chat" in state.response.message


def test_provider_unconfigured_error_is_redacted_under_provider_smoke() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai",
            "OPENAI_API_KEY": "",
        }
    )

    result = create_image_generation_adapter(config).generate(ImageGenerationInput(prompt="生成一张海报"))
    rendered = str(result.model_dump(mode="json"))

    assert result.status == "failed"
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
    assert "sk-runtime-profile-test" not in rendered
