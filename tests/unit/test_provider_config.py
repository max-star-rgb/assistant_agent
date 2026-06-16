from multimodal_agent.config import ProviderConfig, should_run_integration_tests


def test_provider_config_allows_empty_environment() -> None:
    config = ProviderConfig.from_env({})

    assert config.openai_api_key is None
    assert config.qwen_api_key is None
    assert config.seed_api_key is None
    assert config.comfyui_base_url is None
    assert config.chat_provider == "mock"
    assert config.image_generation_provider == "mock"
    assert config.product_search_provider == "mock"
    assert config.price_compare_provider == "mock"
    assert config.render_provider == "mock"
    assert config.video_provider == "mock"
    assert config.intent_router == "rule"
    assert config.has_any_real_provider() is False


def test_provider_config_reads_environment_values() -> None:
    config = ProviderConfig.from_env(
        {
            "OPENAI_API_KEY": "test-openai-key",
            "QWEN_API_KEY": "test-qwen-key",
            "SEED_API_KEY": "test-seed-key",
            "SEED_VISION_BASE_URL": "https://seed.local/vision",
            "SEED_VISION_MODEL": "seed-test-model",
            "COMFYUI_BASE_URL": "http://localhost:8188",
            "BLENDER_RENDER_URL": "http://localhost:9000",
            "SEARCH_API_BASE_URL": "http://localhost:7000",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_CHAT_BASE_URL": "https://qwen.local/v1",
            "QWEN_CHAT_MODEL": "qwen-test-chat",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "comfyui",
            "OPENAI_IMAGE_MODEL": "openai-image-test",
            "QWEN_IMAGE_MODEL": "qwen-image-test",
            "LOCAL_IMAGE_BASE_URL": "http://localhost:8189",
            "LOCAL_IMAGE_MODEL": "local-image-test",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "http",
            "PRODUCT_SEARCH_BASE_URL": "http://localhost:7001",
            "PRODUCT_SEARCH_API_KEY": "test-product-key",
            "PRODUCT_SEARCH_TIMEOUT_SECONDS": "3.5",
            "MULTIMODAL_AGENT_PRICE_PROVIDER": "http",
            "PRICE_COMPARE_BASE_URL": "http://localhost:7002",
            "PRICE_COMPARE_API_KEY": "test-price-key",
            "PRICE_COMPARE_TIMEOUT_SECONDS": "4.5",
            "MULTIMODAL_AGENT_RENDER_PROVIDER": "http",
            "RENDER_BASE_URL": "http://localhost:7003",
            "RENDER_API_KEY": "test-render-key",
            "RENDER_TIMEOUT_SECONDS": "5.5",
            "MULTIMODAL_AGENT_VIDEO_PROVIDER": "http",
            "VIDEO_UNDERSTANDING_BASE_URL": "http://localhost:7004",
            "VIDEO_UNDERSTANDING_API_KEY": "test-video-key",
            "VIDEO_UNDERSTANDING_MODEL": "video-test-model",
            "VIDEO_UNDERSTANDING_TIMEOUT_SECONDS": "6.5",
            "MULTIMODAL_AGENT_MAX_VIDEO_BYTES": "1024",
            "MULTIMODAL_AGENT_MAX_VIDEO_SECONDS": "12.5",
            "MULTIMODAL_AGENT_INTENT_ROUTER": "hybrid",
        }
    )

    assert config.openai_api_key == "test-openai-key"
    assert config.qwen_api_key == "test-qwen-key"
    assert config.seed_api_key == "test-seed-key"
    assert config.seed_vision_base_url == "https://seed.local/vision"
    assert config.seed_vision_model == "seed-test-model"
    assert config.comfyui_base_url == "http://localhost:8188"
    assert config.blender_render_url == "http://localhost:9000"
    assert config.search_api_base_url == "http://localhost:7000"
    assert config.chat_provider == "qwen"
    assert config.qwen_chat_base_url == "https://qwen.local/v1"
    assert config.qwen_chat_model == "qwen-test-chat"
    assert config.image_generation_provider == "comfyui"
    assert config.openai_image_model == "openai-image-test"
    assert config.qwen_image_model == "qwen-image-test"
    assert config.local_image_base_url == "http://localhost:8189"
    assert config.local_image_model == "local-image-test"
    assert config.product_search_provider == "http"
    assert config.product_search_base_url == "http://localhost:7001"
    assert config.product_search_api_key == "test-product-key"
    assert config.product_search_timeout_seconds == 3.5
    assert config.price_compare_provider == "http"
    assert config.price_compare_base_url == "http://localhost:7002"
    assert config.price_compare_api_key == "test-price-key"
    assert config.price_compare_timeout_seconds == 4.5
    assert config.render_provider == "http"
    assert config.render_base_url == "http://localhost:7003"
    assert config.render_api_key == "test-render-key"
    assert config.render_timeout_seconds == 5.5
    assert config.video_provider == "http"
    assert config.video_understanding_base_url == "http://localhost:7004"
    assert config.video_understanding_api_key == "test-video-key"
    assert config.video_understanding_model == "video-test-model"
    assert config.video_understanding_timeout_seconds == 6.5
    assert config.max_video_bytes == 1024
    assert config.max_video_seconds == 12.5
    assert config.intent_router == "hybrid"
    assert config.has_any_real_provider() is True


def test_provider_config_reads_openai_compatible_base_urls() -> None:
    config = ProviderConfig.from_env({})

    assert config.openai_vision_base_url == "https://api.openai.com/v1"
    assert config.qwen_vision_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_integration_tests_are_opt_in() -> None:
    assert should_run_integration_tests({}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "0"}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "1"}) is True
