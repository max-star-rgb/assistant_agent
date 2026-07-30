"""Controlled timeout Environment for AMap weather."""

from __future__ import annotations

from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.recovery import classify_error
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


class AmapWeatherProviderFailureEnvironment:
    """Read-only AMap weather tool with a deterministic provider timeout."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._runner = _TimeoutWeatherRunner()

    def describe(self) -> dict[str, Any]:
        registry = self._build_registry()
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:amap-weather-timeout-v1",
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
                "controlled_timeout_fixture": rule_assertion(
                    not fixture.success
                    and classify_error(fixture.error or "")
                    == "provider_timeout"
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
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        if available_tools is not None:
            subset = ToolRegistry()
            for name in dict.fromkeys([*available_tools, WEATHER_TOOL]):
                subset.register(
                    registry.get(name),
                    registry.registration_record(name),
                )
            subset.seal()
            registry = subset
        return outcome_expectations(
            registry,
            required_failures={WEATHER_TOOL: "provider_timeout"},
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
