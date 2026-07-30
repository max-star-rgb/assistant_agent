"""Controlled Environment for missing AMap weather city."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.task_support import (
    execute_isolated_runtime,
    outcome_expectations,
)
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


class AmapWeatherMissingCityEnvironment:
    """Weather is available, but the user has not supplied its city input."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._runner = _AvailableWeatherRunner()

    def describe(self) -> dict[str, Any]:
        registry = self._build_registry()
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:amap-weather-available-v1",
            "tool_catalog": "real-shaped-catalog-with-controlled-amap-weather",
            "registered_tool_count": len(registry.list()),
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        fixture = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_weather",
            tool_input={"city": "上海市"},
        )
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and WEATHER_TOOL in registry.list()
                    and "weather" not in registry.list()
                    and {"web_search", "web_fetch"}.isdisjoint(
                        registry.list()
                    ),
                    f"registered_tools={registry.list()}",
                    label="真实形态目录只包含高德天气工具",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations}
                    == set(registry.list()),
                    f"expectation_count={len(expectations)}",
                    label="工具结果预期覆盖注册表",
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
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        if available_tools is not None:
            subset = ToolRegistry()
            for name in dict.fromkeys(available_tools):
                subset.register(
                    registry.get(name),
                    registry.registration_record(name),
                )
            subset.seal()
            registry = subset
        return outcome_expectations(registry)

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest | dict[str, Any],
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        self.validate().require_valid()
        return execute_isolated_runtime(
            task=task,
            request=UserRequest.model_validate(request),
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            config=self.config,
            registry=self._build_registry(),
            chat_adapter=self.chat_adapter,
            initial_state={},
        )

    def _build_registry(self) -> ToolRegistry:
        return build_travel_registry(
            definitions=[maps_weather_definition()],
            runner=self._runner,
        )
