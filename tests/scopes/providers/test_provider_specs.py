from assistant_agent.schemas.provider_specs import (
    CHAT_PROVIDER_SPECS,
    IMAGE_GENERATION_PROVIDER_SPECS,
    VISION_PROVIDER_SPECS,
    resolve_image_generation_provider,
    resolve_chat_provider,
    resolve_vision_provider,
    select_chat_provider,
    select_image_generation_provider,
    select_vision_provider,
    supported_chat_providers,
    supported_image_generation_providers,
    supported_vision_providers,
)
from assistant_agent.services.chat_adapter import chat_capabilities_for_provider


def test_chat_provider_specs_include_openai_compatible_providers() -> None:
    assert {"mock", "openai", "qwen", "ark", "deepseek", "local"}.issubset(supported_chat_providers())
    assert CHAT_PROVIDER_SPECS["qwen"].adapter_kind == "openai_compatible"
    assert CHAT_PROVIDER_SPECS["qwen"].api_key_env == "QWEN_API_KEY"
    assert CHAT_PROVIDER_SPECS["qwen"].base_url_env == "QWEN_CHAT_BASE_URL"
    assert CHAT_PROVIDER_SPECS["qwen"].model_env == "QWEN_CHAT_MODEL"
    assert CHAT_PROVIDER_SPECS["qwen"].default_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert CHAT_PROVIDER_SPECS["qwen"].default_model == "qwen-plus"
    assert CHAT_PROVIDER_SPECS["ark"].adapter_kind == "openai_compatible"
    assert CHAT_PROVIDER_SPECS["ark"].api_key_env == "ARK_CHAT_API_KEY"
    assert CHAT_PROVIDER_SPECS["ark"].base_url_env == "ARK_CHAT_BASE_URL"
    assert CHAT_PROVIDER_SPECS["ark"].model_env == "ARK_CHAT_MODEL"
    assert CHAT_PROVIDER_SPECS["ark"].default_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert CHAT_PROVIDER_SPECS["ark"].default_model is None
    assert CHAT_PROVIDER_SPECS["deepseek"].adapter_kind == "openai_compatible"
    assert CHAT_PROVIDER_SPECS["deepseek"].api_key_env == "DEEPSEEK_CHAT_API_KEY"
    assert CHAT_PROVIDER_SPECS["deepseek"].base_url_env == "DEEPSEEK_CHAT_BASE_URL"
    assert CHAT_PROVIDER_SPECS["deepseek"].model_env == "DEEPSEEK_CHAT_MODEL"
    assert CHAT_PROVIDER_SPECS["deepseek"].default_base_url == "https://api.deepseek.com/v1"
    assert CHAT_PROVIDER_SPECS["deepseek"].default_model == "deepseek-chat"


def test_chat_provider_specs_expose_native_tool_capabilities() -> None:
    for provider in ("openai", "qwen", "ark", "deepseek"):
        capabilities = CHAT_PROVIDER_SPECS[provider].capabilities

        assert capabilities.supports_native_tools is True
        assert capabilities.supports_tool_choice is True
        assert capabilities.supports_response_format is True
        assert capabilities.supports_streaming is True
        assert capabilities.supports_async_streaming is True
        assert capabilities.max_tokens_param == "max_tokens"
        assert capabilities.input_modalities == ("text",)
        assert capabilities.output_modalities == ("text", "tool_calls")


def test_chat_adapter_capabilities_are_read_from_provider_specs() -> None:
    assert chat_capabilities_for_provider("deepseek") == CHAT_PROVIDER_SPECS["deepseek"].capabilities
    assert chat_capabilities_for_provider("ark") == CHAT_PROVIDER_SPECS["ark"].capabilities


def test_select_chat_provider_obeys_runtime_profile_guard() -> None:
    assert select_chat_provider("deepseek", allow_real=False) == "mock"
    assert select_chat_provider("deepseek", allow_real=True) == "deepseek"
    assert select_chat_provider("ark", allow_real=False) == "mock"
    assert select_chat_provider("ark", allow_real=True) == "ark"
    assert select_chat_provider("unknown", allow_real=True) == "mock"


def test_resolve_chat_provider_returns_missing_required_env_names() -> None:
    resolved = resolve_chat_provider("deepseek", {})

    assert resolved.provider == "deepseek"
    assert resolved.adapter_kind == "openai_compatible"
    assert resolved.capabilities == CHAT_PROVIDER_SPECS["deepseek"].capabilities
    assert resolved.base_url == "https://api.deepseek.com/v1"
    assert resolved.model == "deepseek-chat"
    assert resolved.missing_required_env() == ["DEEPSEEK_CHAT_API_KEY"]


def test_resolve_chat_provider_accepts_explicit_deepseek_config() -> None:
    resolved = resolve_chat_provider(
        "deepseek",
        {
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "DEEPSEEK_CHAT_BASE_URL": "https://deepseek.local/v1",
            "DEEPSEEK_CHAT_MODEL": "deepseek-test",
        },
    )

    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://deepseek.local/v1"
    assert resolved.model == "deepseek-test"
    assert resolved.missing_required_env() == []


def test_resolve_chat_provider_accepts_explicit_ark_config() -> None:
    resolved = resolve_chat_provider(
        "ark",
        {
            "ARK_CHAT_API_KEY": "test-ark-key",
            "ARK_CHAT_BASE_URL": "https://ark.local/api/v3",
            "ARK_CHAT_MODEL": "ark-chat-test",
        },
    )

    assert resolved.provider == "ark"
    assert resolved.api_key == "test-ark-key"
    assert resolved.base_url == "https://ark.local/api/v3"
    assert resolved.model == "ark-chat-test"
    assert resolved.missing_required_env() == []


def test_resolve_chat_provider_accepts_qwen_dashscope_key_and_workspace() -> None:
    resolved = resolve_chat_provider(
        "qwen",
        {
            "DASHSCOPE_API_KEY": "dashscope-key",
            "QWEN_CHAT_WORKSPACE_ID": "ws-chat",
        },
    )

    assert resolved.provider == "qwen"
    assert resolved.api_key == "dashscope-key"
    assert resolved.base_url == "https://ws-chat.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    assert resolved.model == "qwen-plus"
    assert resolved.missing_required_env() == []


def test_resolve_chat_provider_accepts_ark_api_key_alias() -> None:
    resolved = resolve_chat_provider(
        "ark",
        {
            "ARK_API_KEY": "legacy-ark-key",
            "ARK_CHAT_MODEL": "ark-chat-test",
        },
    )

    assert resolved.provider == "ark"
    assert resolved.api_key == "legacy-ark-key"
    assert resolved.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert resolved.model == "ark-chat-test"
    assert resolved.missing_required_env() == []


def test_resolve_chat_provider_requires_explicit_ark_model() -> None:
    resolved = resolve_chat_provider("ark", {"ARK_CHAT_API_KEY": "test-ark-key"})

    assert resolved.provider == "ark"
    assert resolved.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert resolved.model is None
    assert resolved.missing_required_env() == ["ARK_CHAT_MODEL"]


def test_vision_provider_specs_include_openai_compatible_providers() -> None:
    assert {"mock", "openai", "qwen", "seed", "ark"}.issubset(supported_vision_providers())
    assert VISION_PROVIDER_SPECS["qwen"].adapter_kind == "openai_compatible"
    assert VISION_PROVIDER_SPECS["qwen"].api_key_env == "QWEN_VISION_API_KEY"
    assert VISION_PROVIDER_SPECS["qwen"].base_url_env == "QWEN_VISION_BASE_URL"
    assert VISION_PROVIDER_SPECS["qwen"].model_env == "QWEN_VISION_MODEL"
    assert VISION_PROVIDER_SPECS["ark"].adapter_kind == "ark_responses"
    assert VISION_PROVIDER_SPECS["ark"].api_key_env == "ARK_VISION_API_KEY"
    assert VISION_PROVIDER_SPECS["ark"].base_url_env == "ARK_VISION_BASE_URL"
    assert VISION_PROVIDER_SPECS["ark"].model_env == "ARK_VISION_MODEL"
    assert VISION_PROVIDER_SPECS["ark"].default_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert VISION_PROVIDER_SPECS["ark"].default_model == "doubao-seed-2-0-lite-260215"


def test_non_chat_provider_specs_expose_modalities_without_native_tool_support() -> None:
    qwen_vision = VISION_PROVIDER_SPECS["qwen"].capabilities
    qwen_image = IMAGE_GENERATION_PROVIDER_SPECS["qwen"].capabilities

    assert qwen_vision.supports_native_tools is False
    assert qwen_vision.input_modalities == ("text", "image")
    assert qwen_vision.output_modalities == ("text",)

    assert qwen_image.supports_native_tools is False
    assert qwen_image.input_modalities == ("text", "image")
    assert qwen_image.output_modalities == ("image",)


def test_select_vision_provider_obeys_runtime_profile_guard() -> None:
    assert select_vision_provider("qwen", allow_real=False) == "mock"
    assert select_vision_provider("qwen", allow_real=True) == "qwen"
    assert select_vision_provider("unknown", allow_real=True) == "mock"


def test_resolve_vision_provider_reports_seed_placeholder_base_url_missing() -> None:
    resolved = resolve_vision_provider("seed", {"SEED_API_KEY": "test-key"})

    assert resolved.provider == "seed"
    assert resolved.base_url == "https://api.seed.example/v1/vision"
    assert resolved.model == "seed-vision"
    assert resolved.missing_required_env() == ["SEED_VISION_BASE_URL"]


def test_resolve_vision_provider_accepts_explicit_qwen_config() -> None:
    resolved = resolve_vision_provider(
        "qwen",
        {
            "QWEN_VISION_API_KEY": "test-key",
            "QWEN_VISION_BASE_URL": "https://qwen.local/v1",
            "QWEN_VISION_MODEL": "qwen-vl-test",
        },
    )

    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://qwen.local/v1"
    assert resolved.model == "qwen-vl-test"
    assert resolved.missing_required_env() == []


def test_resolve_vision_provider_accepts_ark_defaults() -> None:
    resolved = resolve_vision_provider("ark", {"ARK_VISION_API_KEY": "test-ark-key"})

    assert resolved.provider == "ark"
    assert resolved.api_key == "test-ark-key"
    assert resolved.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert resolved.model == "doubao-seed-2-0-lite-260215"
    assert resolved.missing_required_env() == []


def test_image_generation_provider_specs_include_optional_skeleton_providers() -> None:
    assert {"mock", "openai", "qwen", "ark", "comfyui", "local"}.issubset(supported_image_generation_providers())
    assert IMAGE_GENERATION_PROVIDER_SPECS["openai"].api_key_env == "OPENAI_API_KEY"
    assert IMAGE_GENERATION_PROVIDER_SPECS["openai"].model_env == "OPENAI_IMAGE_MODEL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].adapter_kind == "dashscope_image"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].api_key_env == "QWEN_IMAGE_API_KEY"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].base_url_env == "QWEN_IMAGE_BASE_URL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].default_base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].default_model == "qwen-image-2.0-pro"
    assert IMAGE_GENERATION_PROVIDER_SPECS["ark"].adapter_kind == "ark_image"
    assert IMAGE_GENERATION_PROVIDER_SPECS["ark"].api_key_env == "ARK_IMAGE_API_KEY"
    assert IMAGE_GENERATION_PROVIDER_SPECS["ark"].base_url_env == "ARK_IMAGE_BASE_URL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["ark"].default_base_url is None
    assert IMAGE_GENERATION_PROVIDER_SPECS["ark"].default_model is None
    assert IMAGE_GENERATION_PROVIDER_SPECS["comfyui"].base_url_env == "COMFYUI_BASE_URL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["local"].adapter_kind == "local_http"


def test_select_image_generation_provider_obeys_runtime_profile_guard() -> None:
    assert select_image_generation_provider("openai", allow_real=False) == "mock"
    assert select_image_generation_provider("openai", allow_real=True) == "openai"
    assert select_image_generation_provider("unknown", allow_real=True) == "mock"


def test_resolve_image_generation_provider_returns_missing_required_env_names() -> None:
    resolved = resolve_image_generation_provider("local", {})

    assert resolved.provider == "local"
    assert resolved.model == "local-image"
    assert resolved.missing_required_env() == ["LOCAL_IMAGE_BASE_URL"]


def test_resolve_ark_image_generation_provider_requires_env_url_and_model() -> None:
    resolved = resolve_image_generation_provider("ark", {"ARK_IMAGE_API_KEY": "test-key"})

    assert resolved.provider == "ark"
    assert resolved.api_key == "test-key"
    assert resolved.base_url is None
    assert resolved.model is None
    assert resolved.missing_required_env() == ["ARK_IMAGE_BASE_URL", "ARK_IMAGE_MODEL"]


def test_resolve_qwen_image_generation_provider_uses_dashscope_key() -> None:
    resolved = resolve_image_generation_provider("qwen", {"QWEN_IMAGE_API_KEY": "test-key"})

    assert resolved.provider == "qwen"
    assert resolved.adapter_kind == "dashscope_image"
    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert resolved.model == "qwen-image-2.0-pro"
    assert resolved.missing_required_env() == []


def test_resolve_ark_image_generation_provider_exposes_adapter_kind() -> None:
    resolved = resolve_image_generation_provider(
        "ark",
        {
            "ARK_IMAGE_API_KEY": "test-key",
            "ARK_IMAGE_BASE_URL": "https://ark.local/api/v3",
            "ARK_IMAGE_MODEL": "ark-image-test",
        },
    )

    assert resolved.adapter_kind == "ark_image"
