"""PyCharm-runnable fixed-input smoke for calendar_create."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import CalendarCreateTool


FIXED_INPUT = {
    "title": "Tool 固定输入日历事件",
    "start_time": "2030-01-15T09:00:00+08:00",
    "end_time": "2030-01-15T09:30:00+08:00",
    "timezone": "Asia/Shanghai",
}


if __name__ == "__main__":
    raise SystemExit(run_tool_smoke(CalendarCreateTool(), FIXED_INPUT))
