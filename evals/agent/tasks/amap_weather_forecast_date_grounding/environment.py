"""Controlled AMap multi-day forecast Environment."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import AssertionResult
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.travel_support import (
    AMAP_SERVER_NAME,
    WEATHER_TOOL,
    build_travel_registry,
    maps_weather_definition,
)


class _ForecastWeatherRunner:
    def __init__(self, *, today: date | None = None) -> None:
        self.today = today or date.today()

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME or tool_name != "maps_weather":
            raise ValueError("unsupported controlled AMap tool")
        payload = self._payload(str(tool_input.get("city") or ""))
        return ToolResult(
            tool_name=WEATHER_TOOL,
            success=True,
            data=payload,
            model_observation=payload,
            output_ref="eval://amap/weather/shanghai-forecast",
        )

    def _payload(self, city: str) -> dict[str, Any]:
        if city != "上海市":
            return {
                "city": city,
                "forecasts": [],
                "reporttime": _report_time(self.today),
            }
        return {
            "city": "上海市",
            "adcode": "310000",
            "province": "上海",
            "reporttime": _report_time(self.today),
            "forecasts": [
                _forecast(
                    self.today,
                    dayweather="多云",
                    nightweather="晴",
                    daytemp="34",
                    nighttemp="27",
                ),
                _forecast(
                    self.today + timedelta(days=1),
                    dayweather="中雨",
                    nightweather="小雨",
                    daytemp="30",
                    nighttemp="25",
                ),
                _forecast(
                    self.today + timedelta(days=2),
                    dayweather="晴",
                    nightweather="多云",
                    daytemp="33",
                    nighttemp="26",
                ),
            ],
        }


class AmapWeatherForecastEnvironment(ControlledTaskEnvironment):
    """Read-only AMap forecast fixture with adjacent-date distractors."""

    dependency_label = "controlled:amap-weather-forecast-v1"
    tool_catalog_label = "real-shaped-catalog-with-controlled-amap-weather"

    def setup(self) -> None:
        self._runner = _ForecastWeatherRunner()

    def required_successes(self) -> tuple[str, ...]:
        return (WEATHER_TOOL,)

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> dict[str, AssertionResult]:
        fixture = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_weather",
            tool_input={"city": "上海市"},
        )
        forecasts = fixture.data.get("forecasts", []) if fixture.data else []
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        return {
            "full_tool_registry": rule_assertion(
                WEATHER_TOOL in registry.list()
                and "weather" not in registry.list()
                and {"web_search", "web_fetch"}.isdisjoint(registry.list())
                and registry.get_spec(WEATHER_TOOL).input_schema.get("required")
                == ["city"],
                f"registered_tools={registry.list()}",
                label="真实形态目录只包含高德天气工具",
            ),
            "controlled_forecast_fixture": rule_assertion(
                fixture.success
                and len(forecasts) == 3
                and forecasts[1].get("date") == tomorrow
                and forecasts[1].get("dayweather") == "中雨"
                and forecasts[1].get("nightweather") == "小雨",
                f"forecast_dates={[item.get('date') for item in forecasts]}",
                label="受控多日预报包含明确明日证据",
            ),
            "isolated_state_boundary": rule_assertion(
                registry.get_spec(WEATHER_TOOL).category == "read",
                "writes=False, state=in-memory-per-run",
                label="天气工具只读且任务状态隔离",
            ),
        }

    def build_registry(self) -> ToolRegistry:
        return build_travel_registry(
            definitions=[maps_weather_definition()],
            runner=self._runner,
        )


def _forecast(
    forecast_date: date,
    *,
    dayweather: str,
    nightweather: str,
    daytemp: str,
    nighttemp: str,
) -> dict[str, str]:
    return {
        "date": forecast_date.isoformat(),
        "week": str(forecast_date.isoweekday()),
        "dayweather": dayweather,
        "nightweather": nightweather,
        "daytemp": daytemp,
        "nighttemp": nighttemp,
        "daywind": "东南",
        "nightwind": "东南",
        "daypower": "1-3",
        "nightpower": "1-3",
    }


def _report_time(current_date: date) -> str:
    return datetime.combine(
        current_date,
        datetime.min.time().replace(hour=11),
        tzinfo=timezone(timedelta(hours=8)),
    ).isoformat()
