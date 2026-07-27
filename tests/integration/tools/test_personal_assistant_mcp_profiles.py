"""Observable translations for supported real personal-assistant MCP profiles."""

from __future__ import annotations

import json
from typing import Any

# Initialize the package through its normal runtime import path before importing
# the MCP-backed service; direct leaf imports currently cross tools.__init__.
from assistant_agent.tools.registry import ToolRegistry as _ToolRegistry  # noqa: F401
from assistant_agent.mcp.sdk_client import _sanitize_sdk_content
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.models import (
    CalendarCreateRequest,
    CalendarSearchRequest,
    WeatherRequest,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.backend import (
    MCPPersonalAssistantCalendarAdapter,
    MCPPersonalAssistantToolBinding,
    MCPPersonalAssistantWeatherAdapter,
)


class RecordingRunner:
    def __init__(self, results: dict[str, ToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        self.calls.append((server_name, tool_name, tool_input))
        return self.results[tool_name]


def _text_result(tool_name: str, text: str) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={"content": [{"type": "text", "text": text}]},
        model_observation={"summary": text[:300]},
        output_ref=f"mcp://test/{tool_name}",
    )


def test_mcp_runtime_content_preserves_large_sanitized_payload_for_adapter_projection() -> None:
    raw_text = "prefix " + ("weather-data " * 2_000)

    content = _sanitize_sdk_content([{"type": "text", "text": raw_text}])

    assert content[0]["text"] == raw_text.strip()


def test_weather_profile_translates_date_range_and_aggregates_hourly_result() -> None:
    weather_data = {
        "city": "Beijing",
        "start_date": "2026-07-22",
        "end_date": "2026-07-23",
        "weather_data": [
            {
                "time": "2026-07-22T00:00",
                "temperature_c": 24.0,
                "weather_description": "Clear sky",
                "precipitation_probability_percent": 10,
            },
            {
                "time": "2026-07-22T12:00",
                "temperature_c": 32.0,
                "weather_description": "Clear sky",
                "precipitation_probability_percent": 20,
            },
            {
                "time": "2026-07-23T00:00",
                "temperature_c": 26.0,
                "weather_description": "Rain",
                "precipitation_probability_percent": 80,
            },
        ],
    }
    text = (
        "forecast\n=== WEATHER DATA ===\n"
        + json.dumps(weather_data)
        + "\n=== ANALYSIS INSTRUCTIONS ===\nsummary"
    )
    runner = RecordingRunner(
        {"get_weather_byDateTimeRange": _text_result("get_weather_byDateTimeRange", text)}
    )
    adapter = MCPPersonalAssistantWeatherAdapter(
        binding=MCPPersonalAssistantToolBinding(
            server_name="weather",
            tool_name="get_weather_byDateTimeRange",
            namespaced_tool_name="mcp__weather__get_weather_byDateTimeRange",
            profile="mcp_weather_server_v1",
        ),
        runner=runner,
    )

    result = adapter.lookup(
        WeatherRequest(
            location="Beijing",
            target_date="2026-07-22/2026-07-23",
        )
    )

    assert runner.calls[0][2] == {
        "city": "Beijing",
        "start_date": "2026-07-22",
        "end_date": "2026-07-23",
    }
    assert result.success is True
    assert [item.model_dump() for item in result.forecast] == [
        {
            "date": "2026-07-22",
            "condition": "Clear sky",
            "temperature_c": 28,
            "high_c": 32,
            "low_c": 24,
            "precipitation_chance": 0.2,
        },
        {
            "date": "2026-07-23",
            "condition": "Rain",
            "temperature_c": 26,
            "high_c": 26,
            "low_c": 26,
            "precipitation_chance": 0.8,
        },
    ]


def test_weather_profile_rejects_success_envelope_without_forecast_data() -> None:
    runner = RecordingRunner(
        {
            "get_weather_byDateTimeRange": _text_result(
                "get_weather_byDateTimeRange",
                "Error: city was not found",
            )
        }
    )
    adapter = MCPPersonalAssistantWeatherAdapter(
        binding=MCPPersonalAssistantToolBinding(
            server_name="weather",
            tool_name="get_weather_byDateTimeRange",
            namespaced_tool_name="mcp__weather__get_weather_byDateTimeRange",
            profile="mcp_weather_server_v1",
        ),
        runner=runner,
    )

    result = adapter.lookup(
        WeatherRequest(location="missing-city", target_date="2026-07-22")
    )

    assert result.success is False
    assert result.errors[0]["code"] == "provider_unsupported_input"


def test_weather_profile_declares_english_location_input() -> None:
    adapter = MCPPersonalAssistantWeatherAdapter(
        binding=MCPPersonalAssistantToolBinding(
            server_name="weather",
            tool_name="get_weather_byDateTimeRange",
            namespaced_tool_name="mcp__weather__get_weather_byDateTimeRange",
            profile="mcp_weather_server_v1",
        ),
        runner=RecordingRunner({}),
    )
    assert adapter.location_input_language == "en"


def test_weather_profile_classifies_upstream_503_as_unavailable() -> None:
    runner = RecordingRunner(
        {
            "get_weather_byDateTimeRange": _text_result(
                "get_weather_byDateTimeRange",
                "Error: Weather API returned status 503",
            )
        }
    )
    adapter = MCPPersonalAssistantWeatherAdapter(
        binding=MCPPersonalAssistantToolBinding(
            server_name="weather",
            tool_name="get_weather_byDateTimeRange",
            namespaced_tool_name="mcp__weather__get_weather_byDateTimeRange",
            profile="mcp_weather_server_v1",
        ),
        runner=runner,
    )

    result = adapter.lookup(
        WeatherRequest(location="Beijing", target_date="2026-07-22")
    )

    assert result.success is False
    assert result.errors[0]["code"] == "provider_unavailable"


def test_workspace_profile_translates_calendar_search_and_create() -> None:
    search_text = (
        "Successfully retrieved 1 events from calendar 'primary':\n"
        '- "Product sync" (Starts: 2026-07-22T10:00:00+08:00, '
        "Ends: 2026-07-22T10:30:00+08:00) ID: event-1 | Link: https://example.test/event-1"
    )
    runner = RecordingRunner(
        {
            "get_events": _text_result("get_events", search_text),
            "manage_event": _text_result(
                "manage_event",
                "Successfully created event 'Planning' for user@example.com.",
            ),
        }
    )
    search_binding = MCPPersonalAssistantToolBinding(
        server_name="google_workspace",
        tool_name="get_events",
        namespaced_tool_name="mcp__google_workspace__get_events",
        profile="workspace_mcp_v1",
        calendar_user_email="user@example.com",
    )
    create_binding = MCPPersonalAssistantToolBinding(
        server_name="google_workspace",
        tool_name="manage_event",
        namespaced_tool_name="mcp__google_workspace__manage_event",
        profile="workspace_mcp_v1",
        calendar_user_email="user@example.com",
    )
    adapter = MCPPersonalAssistantCalendarAdapter(
        runner=runner,
        search_binding=search_binding,
        create_binding=create_binding,
    )

    search_result = adapter.search(
        CalendarSearchRequest(
            query="sync",
            start_time="2026-07-22T00:00:00+08:00",
            end_time="2026-07-23T00:00:00+08:00",
            limit=5,
        )
    )
    create_result = adapter.create(
        CalendarCreateRequest(
            title="Planning",
            start_time="2026-07-22T14:00:00+08:00",
            timezone="Asia/Shanghai",
        )
    )

    assert runner.calls[0][2] == {
        "user_google_email": "user@example.com",
        "query": "sync",
        "time_min": "2026-07-22T00:00:00+08:00",
        "time_max": "2026-07-23T00:00:00+08:00",
        "max_results": 5,
    }
    assert search_result.events[0].event_id == "event-1"
    assert search_result.events[0].title == "Product sync"
    assert runner.calls[1][2] == {
        "user_google_email": "user@example.com",
        "action": "create",
        "summary": "Planning",
        "start_time": "2026-07-22T14:00:00+08:00",
        "end_time": "2026-07-22T15:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "location": None,
        "attendees": None,
        "description": None,
    }
    assert create_result.success is True
    assert create_result.side_effect_level == "committed"
