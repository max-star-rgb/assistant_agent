"""Core local tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.core.python_interpreter import PythonInterpreterTool
from assistant_agent.tools.plugins.core.tool_search import ToolSearchTool


class CoreToolPlugin:
    plugin_id = "core"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [
            ToolSearchTool(
                server_configs=context.mcp_server_configs,
                runner=context.mcp_runner,
            ),
            PythonInterpreterTool(),
        ]
