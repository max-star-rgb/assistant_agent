from dataclasses import asdict

import pytest

from assistant_agent.config import AppConfig, ProviderConfig, load_app_config
from assistant_agent.native_agent.memory import create_memory_backend
from assistant_agent.native_agent.providers import create_chat_model


def _flatten(config: AppConfig) -> dict[str, object]:
    values: dict[str, object] = {"provider_mode": config.provider_mode}
    for section_name in ("runtime", "chat", "vision", "memory", "media"):
        values.update(asdict(getattr(config, section_name)))
    values.update(
        {
            "local_file_access_root": config.tools.local_file_access_root,
            "durable_tasks_enabled": config.tools.durable_tasks_enabled,
        }
    )
    for section_name in ("image_generation", "search", "shopping", "lodging"):
        values.update(asdict(getattr(config.tools, section_name)))
    return values


@pytest.mark.parametrize(
    "env",
    [
        {},
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "DASHSCOPE_API_KEY": "qwen-sentinel",
            "QWEN_CHAT_MODEL": "chat-sentinel",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
        },
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "ark",
            "ARK_API_KEY": "ark-sentinel",
            "ARK_CHAT_MODEL": "ark-chat-sentinel",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_IMAGE_MODEL": "ark-image-sentinel",
        },
    ],
)
def test_nested_config_matches_legacy_effective_values(
    env: dict[str, str],
) -> None:
    assert _flatten(load_app_config(env)) == asdict(ProviderConfig.from_env(env))


INVALID_ENVIRONMENTS = [
    {"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
    {"MEMORY_BACKEND": "unknown"},
    {"REALTIME_KEYFRAME_SEMANTIC_THRESHOLD": "1.1"},
    {
        "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
        "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
    },
    {"REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1"},
]


@pytest.mark.parametrize("env", INVALID_ENVIRONMENTS)
def test_nested_config_preserves_legacy_validation_error(
    env: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as old_error:
        ProviderConfig.from_env(env)
    with pytest.raises(ValueError) as new_error:
        load_app_config(env)
    assert str(new_error.value) == str(old_error.value)


def test_app_config_defaults_are_nested_and_mock_safe() -> None:
    config = AppConfig()
    assert config.provider_mode == "mock"
    assert config.chat.chat_provider == "mock"
    assert config.vision.vision_provider == "mock"
    assert config.tools.image_generation.image_generation_provider == "mock"


def test_chat_and_memory_factories_accept_only_projected_config() -> None:
    """Removing projected inputs must break offline factory construction."""

    config = load_app_config({})

    model = create_chat_model(config.chat, provider_mode=config.provider_mode)
    backend = create_memory_backend(
        config.memory,
        provider_mode=config.provider_mode,
        chat_config=config.chat,
        media_config=config.media,
        langmem_store=None,
    )

    assert model._llm_type == "assistant-agent-mock"
    assert backend is not None
