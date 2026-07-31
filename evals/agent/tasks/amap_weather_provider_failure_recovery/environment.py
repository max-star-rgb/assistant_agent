"""Controlled timeout Environment for AMap weather."""

from __future__ import annotations

from typing import Any

from assistant_agent.runtime.recovery import classify_error
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


TIMEOUT_ERROR = "provider_timeout: 受控高德天气服务超时，当前没有可用预报。"


class _TimeoutWeatherRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME or tool_name != "maps_weather":
            raise ValueError("unsupported controlled AMap tool")
        return ToolResult(
            tool_name=WEATHER_TOOL,
            success=False,
            data={
                "city": str(tool_input.get("city") or ""),
                "forecasts": [],
                "provider": "eval:amap-weather-timeout-v1",
            },
            model_observation={
                "status": "failed",
                "summary": "受控高德天气服务超时，当前没有可用预报。",
            },
            error=TIMEOUT_ERROR,
            output_ref="eval://amap/weather/provider-timeout",
        )


class AmapWeatherProviderFailureEnvironment(ControlledTaskEnvironment):
    """Read-only AMap weather tool with a deterministic provider timeout."""

    dependency_label = "controlled:amap-weather-timeout-v1"
    tool_catalog_label = "real-shaped-catalog-with-controlled-amap-weather"

    def setup(self) -> None:
        self._runner = _TimeoutWeatherRunner()

    def required_failures(self) -> dict[str, str]:
        return {WEATHER_TOOL: "provider_timeout"}

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
            "controlled_timeout_fixture": rule_assertion(
                not fixture.success
                and classify_error(fixture.error or "") == "provider_timeout"
                and fixture.data is not None
                and fixture.data.get("forecasts") == [],
                f"error={fixture.error}, data={fixture.data}",
                label="受控高德超时故障稳定可识别",
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
