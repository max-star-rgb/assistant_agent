from assistant_agent.config import ProviderConfig, should_run_integration_tests


def test_provider_config_allows_empty_environment() -> None:
    config = ProviderConfig.from_env({})

    assert config.runtime_profile.name == "local_demo"
    assert config.openai_api_key is None
    assert config.qwen_api_key is None
    assert config.seed_api_key is None
    assert config.comfyui_base_url is None
    assert config.chat_provider == "mock"
    assert config.chat_stream is False
    assert config.image_generation_provider == "mock"
    assert config.search_provider == "mock"
    assert config.web_search_base_url is None
    assert config.web_search_api_key is None
    assert config.product_search_provider == "mock"
    assert config.price_compare_provider == "mock"
    assert config.render_provider == "mock"
    assert config.video_provider == "mock"
    assert config.intent_router == "rule"
    assert config.conversation_history_backend == "memory"
    assert config.langgraph_checkpointer_backend == "memory"
    assert config.has_any_real_provider() is False


def test_provider_config_auto_persists_conversation_history_with_jsonl_memory() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "jsonl",
            "MULTIMODAL_AGENT_MEMORY_PATH": ".local/memory/long_term_memories.jsonl",
        }
    )

    assert config.memory_backend == "jsonl"
    assert config.conversation_history_backend == "jsonl"
    assert config.conversation_history_path == ".local/memory/conversation_history.jsonl"


def test_provider_config_reads_environment_values() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "OPENAI_API_KEY": "test-openai-key",
            "QWEN_API_KEY": "test-qwen-key",
            "QWEN_VISION_API_KEY": "test-qwen-vision-key",
            "QWEN_IMAGE_API_KEY": "test-qwen-image-key",
            "SEED_API_KEY": "test-seed-key",
            "SEED_VISION_BASE_URL": "https://seed.local/vision",
            "SEED_VISION_MODEL": "seed-test-model",
            "COMFYUI_BASE_URL": "http://localhost:8188",
            "BLENDER_RENDER_URL": "http://localhost:9000",
            "SEARCH_API_BASE_URL": "http://localhost:7000",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_CHAT_BASE_URL": "https://qwen.local/v1",
            "QWEN_CHAT_MODEL": "qwen-test-chat",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
            "DEEPSEEK_CHAT_BASE_URL": "https://deepseek.local/v1",
            "DEEPSEEK_CHAT_MODEL": "deepseek-test-chat",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "comfyui",
            "OPENAI_IMAGE_MODEL": "openai-image-test",
            "QWEN_IMAGE_BASE_URL": "https://dashscope.local/api/v1",
            "QWEN_IMAGE_MODEL": "qwen-image-test",
            "QWEN_IMAGE_DEFAULT_SIZE": "256*256",
            "LOCAL_IMAGE_BASE_URL": "http://localhost:8189",
            "LOCAL_IMAGE_MODEL": "local-image-test",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "WEB_SEARCH_BASE_URL": "http://localhost:7005",
            "WEB_SEARCH_API_KEY": "test-web-search-key",
            "WEB_SEARCH_TIMEOUT_SECONDS": "4.25",
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
            "MULTIMODAL_AGENT_CONVERSATION_HISTORY_BACKEND": "jsonl",
            "MULTIMODAL_AGENT_CONVERSATION_HISTORY_PATH": ".local/test/conversation.jsonl",
            "MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS": "3",
            "LANGGRAPH_CHECKPOINTER_BACKEND": "memory",
        }
    )

    assert config.runtime_profile.name == "provider_smoke"
    assert config.openai_api_key == "test-openai-key"
    assert config.qwen_api_key == "test-qwen-key"
    assert config.qwen_vision_api_key == "test-qwen-vision-key"
    assert config.qwen_image_api_key == "test-qwen-image-key"
    assert config.dashscope_api_key is None
    assert config.seed_api_key == "test-seed-key"
    assert config.seed_vision_base_url == "https://seed.local/vision"
    assert config.seed_vision_model == "seed-test-model"
    assert config.comfyui_base_url == "http://localhost:8188"
    assert config.blender_render_url == "http://localhost:9000"
    assert config.search_api_base_url == "http://localhost:7000"
    assert config.chat_provider == "qwen"
    assert config.chat_api_key == "test-qwen-key"
    assert config.chat_base_url == "https://qwen.local/v1"
    assert config.chat_model == "qwen-test-chat"
    assert config.chat_adapter_kind == "openai_compatible"
    assert config.qwen_chat_base_url == "https://qwen.local/v1"
    assert config.qwen_chat_model == "qwen-test-chat"
    assert config.deepseek_api_key == "test-deepseek-key"
    assert config.deepseek_chat_base_url == "https://deepseek.local/v1"
    assert config.deepseek_chat_model == "deepseek-test-chat"
    assert config.image_generation_provider == "comfyui"
    assert config.image_generation_base_url == "http://localhost:8188"
    assert config.image_generation_adapter_kind == "comfyui"
    assert config.openai_image_model == "openai-image-test"
    assert config.qwen_image_base_url == "https://dashscope.local/api/v1"
    assert config.qwen_image_model == "qwen-image-test"
    assert config.qwen_image_default_size == "1024*1024"
    assert config.local_image_base_url == "http://localhost:8189"
    assert config.local_image_model == "local-image-test"
    assert config.search_provider == "http"
    assert config.web_search_base_url == "http://localhost:7005"
    assert config.web_search_api_key == "test-web-search-key"
    assert config.web_search_timeout_seconds == 4.25
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
    assert config.conversation_history_backend == "jsonl"
    assert config.conversation_history_path == ".local/test/conversation.jsonl"
    assert config.max_conversation_history_turns == 3
    assert config.langgraph_checkpointer_backend == "memory"
    assert config.has_any_real_provider() is True


def test_provider_config_offline_eval_defaults_to_mock_local_providers() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_RUNTIME_PROFILE": "offline_eval"})

    assert config.runtime_profile.name == "offline_eval"
    assert config.vision_provider == "mock"
    assert config.chat_provider == "mock"
    assert config.image_generation_provider == "mock"
    assert config.search_provider == "mock"
    assert config.product_search_provider == "mock"
    assert config.price_compare_provider == "mock"
    assert config.render_provider == "mock"
    assert config.video_provider == "mock"


def test_provider_smoke_does_not_enable_real_provider_from_key_only() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "QWEN_API_KEY": "test-qwen-key",
        }
    )

    assert config.runtime_profile.name == "provider_smoke"
    assert config.qwen_api_key == "test-qwen-key"
    assert config.vision_provider == "mock"
    assert config.chat_provider == "mock"
    assert config.image_generation_provider == "mock"
    assert config.search_provider == "mock"


def test_provider_smoke_allows_explicit_provider_selection() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
        }
    )

    assert config.runtime_profile.name == "provider_smoke"
    assert config.vision_provider == "qwen"
    assert config.vision_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.vision_model == "qwen-vl-plus"
    assert config.vision_adapter_kind == "openai_compatible"


def test_provider_config_reads_deepseek_chat_provider() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
        }
    )

    assert config.chat_provider == "deepseek"
    assert config.deepseek_api_key == "test-deepseek-key"
    assert config.chat_api_key == "test-deepseek-key"
    assert config.chat_base_url == "https://api.deepseek.com/v1"
    assert config.chat_model == "deepseek-chat"
    assert config.chat_adapter_kind == "openai_compatible"
    assert config.deepseek_chat_base_url == "https://api.deepseek.com/v1"
    assert config.deepseek_chat_model == "deepseek-chat"
    assert config.chat_stream is True


def test_provider_config_reads_common_chat_stream_switch() -> None:
    config = ProviderConfig.from_env({"CHAT_STREAM": "true"})

    assert config.chat_stream is True


def test_provider_config_common_chat_stream_can_disable_deepseek_default_stream() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
            "CHAT_STREAM": "false",
        }
    )

    assert config.chat_provider == "deepseek"
    assert config.chat_stream is False


def test_provider_config_reads_deepseek_chat_stream_override() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
            "CHAT_STREAM": "false",
            "DEEPSEEK_CHAT_STREAM": "true",
        }
    )

    assert config.chat_provider == "deepseek"
    assert config.chat_stream is True


def test_provider_config_deepseek_chat_stream_override_can_disable_common_switch() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
            "CHAT_STREAM": "true",
            "DEEPSEEK_CHAT_STREAM": "false",
        }
    )

    assert config.chat_provider == "deepseek"
    assert config.chat_stream is False


def test_provider_config_accepts_legacy_deepseek_api_key_as_fallback() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "legacy-deepseek-key",
        }
    )

    assert config.deepseek_api_key == "legacy-deepseek-key"
    assert config.chat_api_key == "legacy-deepseek-key"
    assert config.resolved_chat_provider().missing_required_env() == []


def test_provider_config_reads_openai_compatible_base_urls() -> None:
    config = ProviderConfig.from_env({})

    assert config.openai_vision_base_url == "https://api.openai.com/v1"
    assert config.qwen_vision_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_provider_config_reads_qwen_vision_provider_from_spec() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "test-qwen-key",
            "QWEN_VISION_BASE_URL": "https://qwen.local/v1",
            "QWEN_VISION_MODEL": "qwen-vl-test",
        }
    )

    assert config.vision_provider == "qwen"
    assert config.vision_api_key == "test-qwen-key"
    assert config.vision_base_url == "https://qwen.local/v1"
    assert config.vision_model == "qwen-vl-test"
    assert config.vision_adapter_kind == "openai_compatible"
    assert config.resolved_vision_provider().missing_required_env() == []


def test_provider_config_reads_ark_vision_provider_from_spec() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-key",
            "ARK_VISION_BASE_URL": "https://ark.local/api/v3",
            "ARK_VISION_MODEL": "ark-vision-test",
        }
    )

    assert config.vision_provider == "ark"
    assert config.vision_api_key == "test-ark-key"
    assert config.vision_base_url == "https://ark.local/api/v3"
    assert config.vision_model == "ark-vision-test"
    assert config.vision_adapter_kind == "ark_responses"
    assert config.ark_vision_base_url == "https://ark.local/api/v3"
    assert config.ark_vision_model == "ark-vision-test"
    assert config.resolved_vision_provider().missing_required_env() == []


def test_provider_config_cleans_mismatched_trailing_quotes() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "MULTIMODAL_AGENT_VIDEO_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-key",
            "ARK_VISION_BASE_URL": "\"https://ark.local/api/v3'\"",
            "ARK_VISION_MODEL": "ark-vision-test",
        }
    )

    assert config.vision_base_url == "https://ark.local/api/v3"
    assert config.ark_vision_base_url == "https://ark.local/api/v3"
    assert config.video_understanding_base_url == "https://ark.local/api/v3"


def test_provider_config_reads_openai_image_generation_provider_from_spec() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-openai-key",
            "OPENAI_IMAGE_MODEL": "image-test-model",
        }
    )

    assert config.image_generation_provider == "openai"
    assert config.image_generation_api_key == "test-openai-key"
    assert config.image_generation_model == "image-test-model"
    assert config.image_generation_adapter_kind == "openai_image"
    assert config.resolved_image_generation_provider().missing_required_env() == []


def test_provider_config_reads_qwen_image_generation_provider_from_dashscope_spec() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
            "QWEN_IMAGE_API_KEY": "test-qwen-image-key",
            "QWEN_IMAGE_BASE_URL": "https://dashscope.local/api/v1",
            "QWEN_IMAGE_MODEL": "qwen-image-test",
            "QWEN_IMAGE_DEFAULT_SIZE": "256*256",
        }
    )

    assert config.image_generation_provider == "qwen"
    assert config.qwen_image_api_key == "test-qwen-image-key"
    assert config.image_generation_api_key == "test-qwen-image-key"
    assert config.image_generation_base_url == "https://dashscope.local/api/v1"
    assert config.image_generation_model == "qwen-image-test"
    assert config.image_generation_adapter_kind == "dashscope_image"
    assert config.qwen_image_default_size == "1024*1024"
    assert config.resolved_image_generation_provider().missing_required_env() == []


def test_provider_config_reads_ark_image_generation_provider_from_spec() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "ark",
            "ARK_IMAGE_API_KEY": "test-ark-key",
            "ARK_IMAGE_BASE_URL": "https://ark.local/api/v3",
            "ARK_IMAGE_MODEL": "ark-image-test",
            "ARK_IMAGE_DEFAULT_SIZE": "ignored-by-code",
            "ARK_IMAGE_OUTPUT_FORMAT": "ignored-by-code",
        }
    )

    assert config.image_generation_provider == "ark"
    assert config.ark_image_api_key == "test-ark-key"
    assert config.image_generation_api_key == "test-ark-key"
    assert config.image_generation_base_url == "https://ark.local/api/v3"
    assert config.image_generation_model == "ark-image-test"
    assert config.image_generation_adapter_kind == "ark_image"
    assert config.ark_image_default_size == "2K"
    assert config.ark_image_output_format == "png"


def test_integration_tests_are_opt_in() -> None:
    assert should_run_integration_tests({}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "0"}) is False
    assert should_run_integration_tests({"RUN_INTEGRATION_TESTS": "1"}) is True
