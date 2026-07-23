"""Memory tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tools.plugins.memory.tools import MemoryGetTool, MemorySearchTool


class MemoryToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="memory", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [
            MemorySearchTool(),
            MemoryGetTool(),
        ]
