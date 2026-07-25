"""Built-in local text-file access plugin."""

from assistant_agent.tool_plugins.builtin.local_file_access.tool import (
    LocalFileReadTool,
)
from assistant_agent.tool_plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)
from assistant_agent.tools.base import Tool


class LocalFileAccessPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="local_file_access",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [LocalFileReadTool(root=context.config.local_file_access_root)]
