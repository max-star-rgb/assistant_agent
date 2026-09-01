import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from assistant_agent.config import AppConfig, load_app_config
from assistant_agent.media.video.video_adapter import create_video_understanding_adapter
from assistant_agent.native_agent.memory import create_memory_backend
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.providers.provider_selection import create_vision_adapter
from assistant_agent.tools.ids import VISUAL_IMAGE_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME
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


def test_default_config_matches_reviewed_snapshot() -> None:
    expected = json.loads(
        Path(__file__).with_name("expected_default_config.json").read_text()
    )

    assert json.loads(json.dumps(_flatten(load_app_config({})))) == expected


def test_qwen_dashscope_alias_and_workspace_url() -> None:
    config = load_app_config(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "DASHSCOPE_API_KEY": "qwen-sentinel",
            "QWEN_CHAT_WORKSPACE_ID": "workspace-sentinel",
        }
    )

    assert config.chat.qwen_api_key == "qwen-sentinel"
    assert config.chat.chat_api_key == "qwen-sentinel"
    assert config.chat.qwen_chat_base_url == (
        "https://workspace-sentinel.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )


def test_ark_config_uses_shared_api_key() -> None:
    config = load_app_config(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "ark",
            "ARK_API_KEY": "ark-sentinel",
            "ARK_CHAT_MODEL": "ark-chat-sentinel",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "ark",
            "ARK_IMAGE_BASE_URL": "https://ark-image.example/v3",
            "ARK_IMAGE_MODEL": "ark-image-sentinel",
        }
    )

    assert config.chat.chat_api_key == "ark-sentinel"
    assert config.vision.vision_api_key == "ark-sentinel"
    assert config.tools.image_generation.image_generation_api_key == "ark-sentinel"


def test_invalid_context_threshold_preserves_error() -> None:
    with pytest.raises(ValueError, match="context compaction ratios must satisfy"):
        load_app_config(
            {
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
            }
        )


def test_removed_realtime_keyframe_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="removed_realtime_keyframe_config"):
        load_app_config({"REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1"})


@pytest.mark.parametrize("value", ["99", "0"])
def test_visual_memory_result_limit_is_not_an_environment_setting(value: str) -> None:
    assert (
        load_app_config(
            {"VISUAL_MEMORY_RESULT_LIMIT": value}
        ).vision.visual_memory_result_limit
        == 12
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {
                "LANGGRAPH_CHECKPOINTER_BACKEND": "bad",
                "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1",
            },
            "removed_realtime_keyframe_config",
        ),
        (
            {
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
                "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1",
            },
            "removed_realtime_keyframe_config",
        ),
        (
            {
                "MEMORY_BACKEND": "bad",
                "REALTIME_KEYFRAME_SEMANTIC_THRESHOLD": "2",
            },
            "memory backend must be disabled, mem0, or langmem",
        ),
    ],
)
def test_combined_invalid_environment_preserves_legacy_first_error(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        load_app_config(env)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {
                "MULTIMODAL_AGENT_EMBEDDING_PROVIDER": "dashscope",
                "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
                "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1",
            },
            "conflicting_embedding_provider",
        ),
        (
            {
                "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1",
                "QWEN_CHAT_API_PROTOCOL": "bad",
            },
            "removed_realtime_keyframe_config",
        ),
        (
            {
                "QWEN_CHAT_API_PROTOCOL": "bad",
                "LANGGRAPH_CHECKPOINTER_BACKEND": "bad",
            },
            "QWEN_CHAT_API_PROTOCOL must be 'dashscope' or 'openai_compatible'",
        ),
        (
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
                "MEMORY_BACKEND": "bad",
            },
            "MULTIMODAL_AGENT_PROVIDER_MODE=real requires a non-mock "
            "MULTIMODAL_AGENT_CHAT_PROVIDER with complete configuration.",
        ),
        (
            {
                "MEMORY_BACKEND": "bad",
                "REMOTE_VISUAL_MEMORY_ENABLED": "true",
            },
            "memory backend must be disabled, mem0, or langmem",
        ),
        (
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
                "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
                "QWEN_API_KEY": "sentinel",
                "MEMORY_BACKEND": "langmem",
                "REMOTE_VISUAL_MEMORY_ENABLED": "true",
                "REMOTE_VISUAL_MEMORY_BASE_URL": "https://memory.example",
                "REMOTE_VISUAL_MEMORY_QUERY_TIMEOUT_SECONDS": "0",
                "REALTIME_KEYFRAME_SEMANTIC_THRESHOLD": "2",
            },
            "remote visual memory query timeout must be positive",
        ),
        (
            {
                "REALTIME_KEYFRAME_SEMANTIC_THRESHOLD": "2",
                "PROACTIVE_MESSAGE_DELIVERY_TIMEOUT_SECONDS": "0",
            },
            "keyframe semantic threshold must be between 0 and 1",
        ),
        (
            {
                "PROACTIVE_MESSAGE_DELIVERY_TIMEOUT_SECONDS": "0",
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
            },
            "proactive message delivery timeout must be positive",
        ),
        (
            {
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
                "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
                "REALTIME_VISUAL_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
                "REALTIME_VISUAL_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
            },
            "context compaction ratios must satisfy 0 < target < trigger < hard <= 1",
        ),
    ],
)
def test_invalid_environment_preserves_legacy_validation_order(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError) as error:
        load_app_config(env)

    assert str(error.value) == message


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


def test_web_fetch_tool_default_constructor_uses_mock_adapter() -> None:
    assert create_web_fetch_tool().name == WEB_FETCH_TOOL_NAME


def test_visual_image_search_tool_default_constructor_uses_mock_adapter() -> None:
    assert create_visual_image_search_tool().name == VISUAL_IMAGE_SEARCH_TOOL_NAME
