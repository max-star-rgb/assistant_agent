from multimodal_agent.services.provider_specs import (
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


def test_chat_provider_specs_include_openai_compatible_providers() -> None:
    assert {"mock", "openai", "qwen", "deepseek", "local"}.issubset(supported_chat_providers())
    assert CHAT_PROVIDER_SPECS["deepseek"].adapter_kind == "openai_compatible"
    assert CHAT_PROVIDER_SPECS["deepseek"].api_key_env == "DEEPSEEK_API_KEY"
    assert CHAT_PROVIDER_SPECS["deepseek"].base_url_env == "DEEPSEEK_CHAT_BASE_URL"
    assert CHAT_PROVIDER_SPECS["deepseek"].model_env == "DEEPSEEK_CHAT_MODEL"
    assert CHAT_PROVIDER_SPECS["deepseek"].default_base_url == "https://api.deepseek.com/v1"
    assert CHAT_PROVIDER_SPECS["deepseek"].default_model == "deepseek-chat"


def test_select_chat_provider_obeys_runtime_profile_guard() -> None:
    assert select_chat_provider("deepseek", allow_real=False) == "mock"
    assert select_chat_provider("deepseek", allow_real=True) == "deepseek"
    assert select_chat_provider("unknown", allow_real=True) == "mock"


def test_resolve_chat_provider_returns_missing_required_env_names() -> None:
    resolved = resolve_chat_provider("deepseek", {})

    assert resolved.provider == "deepseek"
    assert resolved.base_url == "https://api.deepseek.com/v1"
    assert resolved.model == "deepseek-chat"
    assert resolved.missing_required_env() == ["DEEPSEEK_API_KEY"]


def test_resolve_chat_provider_accepts_explicit_deepseek_config() -> None:
    resolved = resolve_chat_provider(
        "deepseek",
        {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_CHAT_BASE_URL": "https://deepseek.local/v1",
            "DEEPSEEK_CHAT_MODEL": "deepseek-test",
        },
    )

    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://deepseek.local/v1"
    assert resolved.model == "deepseek-test"
    assert resolved.missing_required_env() == []


def test_vision_provider_specs_include_openai_compatible_providers() -> None:
    assert {"mock", "openai", "qwen", "seed"}.issubset(supported_vision_providers())
    assert VISION_PROVIDER_SPECS["qwen"].adapter_kind == "openai_compatible"
    assert VISION_PROVIDER_SPECS["qwen"].api_key_env == "QWEN_API_KEY"
    assert VISION_PROVIDER_SPECS["qwen"].base_url_env == "QWEN_VISION_BASE_URL"
    assert VISION_PROVIDER_SPECS["qwen"].model_env == "QWEN_VISION_MODEL"


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
            "QWEN_API_KEY": "test-key",
            "QWEN_VISION_BASE_URL": "https://qwen.local/v1",
            "QWEN_VISION_MODEL": "qwen-vl-test",
        },
    )

    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://qwen.local/v1"
    assert resolved.model == "qwen-vl-test"
    assert resolved.missing_required_env() == []


def test_image_generation_provider_specs_include_optional_skeleton_providers() -> None:
    assert {"mock", "openai", "qwen", "comfyui", "local"}.issubset(supported_image_generation_providers())
    assert IMAGE_GENERATION_PROVIDER_SPECS["openai"].api_key_env == "OPENAI_API_KEY"
    assert IMAGE_GENERATION_PROVIDER_SPECS["openai"].model_env == "OPENAI_IMAGE_MODEL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].adapter_kind == "dashscope_image"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].api_key_env == "DASHSCOPE_API_KEY"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].base_url_env == "QWEN_IMAGE_BASE_URL"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].default_base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert IMAGE_GENERATION_PROVIDER_SPECS["qwen"].default_model == "qwen-image-2.0-pro"
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


def test_resolve_qwen_image_generation_provider_uses_dashscope_key() -> None:
    resolved = resolve_image_generation_provider("qwen", {"DASHSCOPE_API_KEY": "test-key"})

    assert resolved.provider == "qwen"
    assert resolved.api_key == "test-key"
    assert resolved.base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert resolved.model == "qwen-image-2.0-pro"
    assert resolved.missing_required_env() == []
