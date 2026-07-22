"""Operator-triggered real weather and calendar tool calls."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from assistant_agent.schemas.tool_ids import (
    CALENDAR_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
)


def _json_result(result) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def test_weather_returns_real_provider_result(run_real_tool) -> None:
    result = run_real_tool(
        WEATHER_TOOL_NAME,
        {
            "location": "Beijing",
            "target_date": date.today().isoformat(),
            "days": 1,
            "units": "metric",
        },
    )

    print("REAL_WEATHER_RESULT=" + _json_result(result))
    assert result.success, _json_result(result)
    assert isinstance(result.data, dict), _json_result(result)
    assert str(result.data.get("provider", "")).startswith("mcp:"), _json_result(result)
    assert result.data.get("forecast"), _json_result(result)


def test_calendar_search_returns_real_google_result(run_real_tool) -> None:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    result = run_real_tool(
        CALENDAR_SEARCH_TOOL_NAME,
        {
            "query": "today",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "limit": 5,
        },
    )

    print("REAL_CALENDAR_RESULT=" + _json_result(result))
    assert result.success, _json_result(result)
    assert isinstance(result.data, dict), _json_result(result)
    assert result.data.get("provider") == "mcp:google_workspace.get_events", _json_result(
        result
    )
    assert isinstance(result.data.get("events"), list), _json_result(result)
