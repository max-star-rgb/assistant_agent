"""Lodging search Tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    HotelPriceWatchCreateTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class LodgingToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="lodging", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode:
            return []
        tools: list[Tool] = [LodgingSearchTool()]
        if (
            context.config.durable_tasks_enabled
            and context.durable_task_service is not None
        ):
            tools.append(
                HotelPriceWatchCreateTool(context.durable_task_service)
            )
        return tools
