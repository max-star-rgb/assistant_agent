"""PyCharm-runnable fixed-input smoke for calendar_search."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    create_calendar_search_tool,
)


FIXED_INPUT = {"query": "Product sync"}


if __name__ == "__main__":
    raise SystemExit(run_tool_smoke(create_calendar_search_tool(), FIXED_INPUT))
