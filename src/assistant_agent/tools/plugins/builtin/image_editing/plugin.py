"""Static assembly for the explicitly configured image editing mock."""

from langchain_core.tools import BaseTool

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.image_editing.tool import create_image_editing_tool


class ImageEditingToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        mock_file = context.config.image_generation.image_editing_mock_file
        if context.thread_resource_manager is None or not mock_file:
            return []
        return [create_image_editing_tool(
            mock_file=mock_file, thread_resource_manager=context.thread_resource_manager,
        )]
