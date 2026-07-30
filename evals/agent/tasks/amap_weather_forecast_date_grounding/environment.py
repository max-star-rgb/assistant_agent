"""Controlled AMap multi-day forecast Environment."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


class AmapWeatherForecastEnvironment:
    """Read-only AMap forecast fixture with adjacent-date distractors."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._runner = _ForecastWeatherRunner()

    def describe(self) -> dict[str, Any]:
        registry = self._build_registry()
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:amap-weather-forecast-v1",
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
        forecasts = fixture.data.get("forecasts", []) if fixture.data else []
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and WEATHER_TOOL in registry.list()
                    and "weather" not in registry.list()
                    and {"web_search", "web_fetch"}.isdisjoint(
                        registry.list()
                    )
                    and registry.get_spec(WEATHER_TOOL).input_schema.get(
                        "required"
                    )
                    == ["city"],
                    f"registered_tools={registry.list()}",
                    label="真实形态目录只包含高德天气工具",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations}
                    == set(registry.list()),
                    f"expectation_count={len(expectations)}",
                    label="工具结果预期覆盖注册表",
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
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        if available_tools is not None:
            registry = _subset_registry(
                registry,
                [*available_tools, WEATHER_TOOL],
            )
        return outcome_expectations(
            registry,
            required_successes=(WEATHER_TOOL,),
        )

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


def _subset_registry(
    registry: ToolRegistry,
    names: list[str],
) -> ToolRegistry:
    subset = ToolRegistry()
    for name in dict.fromkeys(names):
        subset.register(
            registry.get(name),
            registry.registration_record(name),
        )
    subset.seal()
    return subset
