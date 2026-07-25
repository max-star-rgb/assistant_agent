"""Deferred Tool catalog discovery plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tool_plugins.builtin.tool_discovery.tool import ToolSearchTool
from assistant_agent.tool_plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class ToolDiscoveryPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="tool_discovery", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [
            ToolSearchTool(
                server_configs=context.mcp_server_configs,
                runner=context.mcp_runner,
            )
        ]
