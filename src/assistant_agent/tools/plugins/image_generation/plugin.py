"""Image generation tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.services.image_generation_adapter import create_image_generation_adapter
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.image_generation.tool import ImageGenerationTool


class ImageGenerationToolPlugin:
    plugin_id = "image_generation"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not image_generation_provider_ready(context.config):
            return []
        return [ImageGenerationTool(adapter=create_image_generation_adapter(context.config))]


def image_generation_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.image_generation_provider != "mock"
        and not config.resolved_image_generation_provider().missing_required_env()
    )
