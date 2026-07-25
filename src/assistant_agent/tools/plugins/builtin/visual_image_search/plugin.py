"""Visual image search plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.visual_image_search.backend import (
    create_visual_image_search_adapter,
)
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    VisualImageSearchTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class VisualImageSearchPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="visual_image_search",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not visual_search_provider_ready(context.config):
            return []
        return [
            VisualImageSearchTool(
                adapter=create_visual_image_search_adapter(context.config)
            )
        ]


def visual_search_provider_ready(config: ProviderConfig) -> bool:
    return bool(
        config.visual_image_search_provider == "qwen"
        and config.qwen_image_search_api_key
    )
