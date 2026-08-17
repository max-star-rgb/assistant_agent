"""Calendar and contacts tools backed by local or MCP adapters."""

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.backend import (
    configured_calendar_contacts_tools,
    create_calendar_contacts_adapter_bundle,
)
from assistant_agent.tools.ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarCreateTool,
    CalendarSearchTool,
    ContactsSearchTool,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)


DEFAULT_LOCAL_CALENDAR_PATH = ".data/calendar/events.sqlite3"


class CalendarContactsPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="calendar_contacts",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        tool_names = configured_calendar_contacts_tools(context.mcp_server_configs)
        if context.calendar_adapter is not None or not context.mock_mode:
            tool_names.update(
                {CALENDAR_SEARCH_TOOL_NAME, CALENDAR_CREATE_TOOL_NAME}
            )
        if not context.mock_mode and not tool_names:
            return []
        adapters = create_calendar_contacts_adapter_bundle(
            context.config,
            mcp_server_configs=context.mcp_server_configs,
            mcp_runner=context.mcp_runner,
        )
        tools: list[BaseTool] = []
        calendar_adapter = (
            context.calendar_adapter
            or (
                LocalSQLiteCalendarAdapter(DEFAULT_LOCAL_CALENDAR_PATH)
                if not context.mock_mode
                else adapters.calendar
            )
        )
        if context.mock_mode or CALENDAR_SEARCH_TOOL_NAME in tool_names:
            tools.append(CalendarSearchTool(adapter=calendar_adapter))
        if context.mock_mode or CALENDAR_CREATE_TOOL_NAME in tool_names:
            tools.append(CalendarCreateTool(adapter=calendar_adapter))
        if context.mock_mode or CONTACTS_SEARCH_TOOL_NAME in tool_names:
            tools.append(ContactsSearchTool(adapter=adapters.contacts))
        return tools
