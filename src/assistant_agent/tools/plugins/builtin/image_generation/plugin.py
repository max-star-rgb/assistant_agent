"""Image generation tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.image_generation.backend import (
    create_image_generation_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    create_image_generation_tool,
)


class ImageGenerationToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if context.thread_resource_manager is None:
            return []
        if not context.mock_mode and not image_generation_provider_ready(
            context.config
        ):
            return []
        return [
            create_image_generation_tool(
                adapter=create_image_generation_adapter(context.config),
                thread_resource_manager=context.thread_resource_manager,
                artifact_base_url=context.config.artifact_base_url,
            )
        ]


def image_generation_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.image_generation_provider != "mock"
        and not config.resolved_image_generation_provider().missing_required_env()
    )
