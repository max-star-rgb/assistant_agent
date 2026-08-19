"""Calendar and contacts tools backed by local business adapters."""

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    MockCalendarAdapter,
    MockContactsAdapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    create_calendar_create_tool,
    create_calendar_search_tool,
    create_contacts_search_tool,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)


DEFAULT_LOCAL_CALENDAR_PATH = ".data/calendar/events.sqlite3"


class CalendarContactsPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        calendar_adapter = context.calendar_adapter
        if calendar_adapter is None:
            calendar_adapter = (
                MockCalendarAdapter()
                if context.mock_mode
                else LocalSQLiteCalendarAdapter(DEFAULT_LOCAL_CALENDAR_PATH)
            )
        tools: list[BaseTool] = [
            create_calendar_search_tool(adapter=calendar_adapter),
            create_calendar_create_tool(adapter=calendar_adapter),
        ]
        if context.mock_mode:
            tools.append(create_contacts_search_tool(adapter=MockContactsAdapter()))
        return tools
