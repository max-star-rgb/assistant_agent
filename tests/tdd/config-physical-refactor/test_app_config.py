import asyncio
from dataclasses import asdict

import pytest

from assistant_agent.config import AppConfig, ProviderConfig, load_app_config
from assistant_agent.media.video.video_adapter import create_video_understanding_adapter
from assistant_agent.native_agent.memory import create_memory_backend
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.providers.provider_selection import create_vision_adapter
from assistant_agent.tools.ids import (
    HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
    VISUAL_IMAGE_SEARCH_TOOL_NAME,
    WEB_FETCH_TOOL_NAME,
)
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    create_visual_image_search_tool,
)
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import (
    create_web_fetch_tool,
)


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


def test_vision_factories_use_projected_config() -> None:
    """Missing projected vision inputs must break offline factory construction."""

    config = load_app_config({})

    adapter = create_vision_adapter(
        config.vision,
        provider_mode=config.provider_mode,
    )
    video = create_video_understanding_adapter(
        config.vision,
        provider_mode=config.provider_mode,
    )

    assert adapter.provider == "mock"
    assert video.provider == "mock"


def test_tool_inventory_uses_projected_config() -> None:
    """Inventory must compose from the config sections it actually consumes."""

    config = load_app_config({})

    tools = asyncio.run(
        create_native_tool_inventory(
            config.tools,
            provider_mode=config.provider_mode,
            vision_config=config.vision,
            media_config=config.media,
            resources=NativeToolResources(),
            mcp_server_configs=[],
        )
    )

    assert tools
    assert len({tool.name for tool in tools}) == len(tools)


def test_legacy_mock_inventory_preserves_projected_tool_config() -> None:
    """The temporary bridge must not discard the legacy caller's tool settings."""

    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock", durable_tasks_enabled=True),
            resources=NativeToolResources(durable_task_service=object()),
            mcp_server_configs=[],
        )
    )

    assert HOTEL_PRICE_WATCH_CREATE_TOOL_NAME in {tool.name for tool in tools}


def test_web_fetch_tool_default_constructor_uses_mock_adapter() -> None:
    assert create_web_fetch_tool().name == WEB_FETCH_TOOL_NAME


def test_visual_image_search_tool_default_constructor_uses_mock_adapter() -> None:
    assert create_visual_image_search_tool().name == VISUAL_IMAGE_SEARCH_TOOL_NAME
