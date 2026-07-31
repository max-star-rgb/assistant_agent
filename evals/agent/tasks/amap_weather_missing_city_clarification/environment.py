"""Controlled Environment for missing AMap weather city."""

from __future__ import annotations

from datetime import date, timedelta
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


class _AvailableWeatherRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME or tool_name != "maps_weather":
            raise ValueError("unsupported controlled AMap tool")
        city = str(tool_input.get("city") or "")
        payload = {
            "city": city,
            "forecasts": [
                {
                    "date": (date.today() + timedelta(days=1)).isoformat(),
                    "dayweather": "晴",
                    "nightweather": "多云",
                    "daytemp": "29",
                    "nighttemp": "23",
                    "daywind": "东",
                    "nightwind": "东",
                    "daypower": "1-3",
                    "nightpower": "1-3",
                }
            ],
        }
        return ToolResult(
            tool_name=WEATHER_TOOL,
            success=True,
            data=payload,
            model_observation=payload,
            output_ref="eval://amap/weather/available",
        )


class AmapWeatherMissingCityEnvironment(ControlledTaskEnvironment):
    """Weather is available, but the user has not supplied its city input."""

    dependency_label = "controlled:amap-weather-available-v1"
    tool_catalog_label = "real-shaped-catalog-with-controlled-amap-weather"

    def setup(self) -> None:
        self._runner = _AvailableWeatherRunner()

    def task_validation_checks(
        self, registry: ToolRegistry
    ) -> dict[str, AssertionResult]:
        fixture = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_weather",
            tool_input={"city": "上海市"},
        )
        return {
            "full_tool_registry": rule_assertion(
                WEATHER_TOOL in registry.list()
                and "weather" not in registry.list()
                and {"web_search", "web_fetch"}.isdisjoint(registry.list()),
                f"registered_tools={registry.list()}",
                label="真实形态目录只包含高德天气工具",
            ),
            "controlled_weather_fixture": rule_assertion(
                fixture.success
                and fixture.data is not None
                and fixture.data.get("city") == "上海市"
                and len(fixture.data.get("forecasts", [])) == 1,
                f"fixture={fixture.data}",
                label="天气依赖可用且不会迫使 Agent 澄清",
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
