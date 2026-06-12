from multimodal_agent.config import ProviderConfig, should_run_integration_tests


def test_provider_config_allows_empty_environment() -> None:
    config = ProviderConfig.from_env({})

    assert config.openai_api_key is None
    assert config.qwen_api_key is None
    assert config.seed_api_key is None
    assert config.comfyui_base_url is None
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
    assert config.has_any_real_provider() is True


def test_provider_config_reads_openai_compatible_base_urls() -> None:
    config = ProviderConfig.from_env({})

    assert config.openai_vision_base_url == "https://api.openai.com/v1"
    assert config.qwen_vision_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_integration_tests_are_opt_in() -> None:
    assert should_run_integration_tests({}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "0"}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "1"}) is True
