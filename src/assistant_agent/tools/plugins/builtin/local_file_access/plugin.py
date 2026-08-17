"""Built-in local text-file access plugin."""

from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    LocalFileReadTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)
from langchain_core.tools import BaseTool


class LocalFileAccessPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="local_file_access",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        return [LocalFileReadTool(root=context.config.local_file_access_root)]
