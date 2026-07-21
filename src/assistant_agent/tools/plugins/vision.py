"""Vision understanding and visual search tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.services.tool_visual_image_search_adapter import (
    create_visual_image_search_adapter,
)
from assistant_agent.services.vision_client import create_vision_understanding_client
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.visual_image_search_tool import VisualImageSearchTool


class VisionToolPlugin:
    plugin_id = "vision"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        tools: list[Tool] = []
        if context.mock_mode or vision_provider_ready(context.config):
            tools.append(
                VisionUnderstandingTool(
                    client=create_vision_understanding_client(context.config),
                    context_store=context.video_context_store,
                    memory_store=context.realtime_video_memory_store,
                )
            )
        if context.mock_mode or visual_search_provider_ready(context.config):
            tools.append(
                VisualImageSearchTool(
                    adapter=create_visual_image_search_adapter(context.config)
                )
            )
        return tools


def vision_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    )


def visual_search_provider_ready(config: ProviderConfig) -> bool:
    return bool(
        config.visual_image_search_provider == "qwen"
        and config.qwen_image_search_api_key
    )
