"""Calendar and contacts tools backed by local business adapters."""

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    MockCalendarAdapter,
    MockContactsAdapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
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
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        calendar_adapter = context.calendar_adapter
        if calendar_adapter is None:
            calendar_adapter = (
                MockCalendarAdapter()
                if context.mock_mode
                else LocalSQLiteCalendarAdapter(DEFAULT_LOCAL_CALENDAR_PATH)
            )
        tools: list[BaseTool] = [
            CalendarSearchTool(adapter=calendar_adapter),
            CalendarCreateTool(adapter=calendar_adapter),
        ]
        if context.mock_mode:
            tools.append(ContactsSearchTool(adapter=MockContactsAdapter()))
        return tools
