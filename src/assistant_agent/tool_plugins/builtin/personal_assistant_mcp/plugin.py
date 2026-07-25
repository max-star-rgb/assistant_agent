"""MCP-backed weather, calendar, and contacts Tool plugin."""

from assistant_agent.services.personal_assistant_mcp_adapters import (
    configured_personal_assistant_tools,
    create_personal_assistant_adapter_bundle,
)
from assistant_agent.schemas.tool_ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
)
from assistant_agent.tools.base import Tool
from assistant_agent.tool_plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tool_plugins.builtin.personal_assistant_mcp.tools import (
    CalendarCreateTool,
    CalendarSearchTool,
    ContactsSearchTool,
    WeatherTool,
)


class PersonalAssistantMCPToolPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="personal_assistant_mcp",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        tool_names = configured_personal_assistant_tools(context.mcp_server_configs)
        if not context.mock_mode and not tool_names:
            return []
        adapters = create_personal_assistant_adapter_bundle(
            context.config,
            mcp_server_configs=context.mcp_server_configs,
            mcp_runner=context.mcp_runner,
        )
        tools: list[Tool] = []
        if context.mock_mode or WEATHER_TOOL_NAME in tool_names:
            tools.append(WeatherTool(adapter=adapters.weather))
        if context.mock_mode or CALENDAR_SEARCH_TOOL_NAME in tool_names:
            tools.append(CalendarSearchTool(adapter=adapters.calendar))
        if context.mock_mode or CALENDAR_CREATE_TOOL_NAME in tool_names:
            tools.append(CalendarCreateTool(adapter=adapters.calendar))
        if context.mock_mode or CONTACTS_SEARCH_TOOL_NAME in tool_names:
            tools.append(ContactsSearchTool(adapter=adapters.contacts))
        return tools
