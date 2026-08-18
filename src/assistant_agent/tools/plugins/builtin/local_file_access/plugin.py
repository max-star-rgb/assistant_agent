"""Built-in local text-file access plugin."""

from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    create_local_file_read_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from langchain_core.tools import BaseTool


class LocalFileAccessPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        return [create_local_file_read_tool(context.config.local_file_access_root)]
