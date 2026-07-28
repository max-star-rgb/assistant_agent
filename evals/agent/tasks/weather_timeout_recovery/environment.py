"""Controlled runtime environment for the weather timeout recovery task."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    WeatherRequest,
    WeatherResult,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    WeatherTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    RunEvidence,
    TaskExecution,
    TaskSpec,
)
from evals.agent.evidence import (
    available_tools,
    provider_result_kinds,
    tool_executions,
    validation_results,
)
from evals.agent.grading import assertion, environment_validation
from evals.agent.provider_gate import validate_real_chat_config


class WeatherTimeoutFixture(BaseModel):
    id: Literal["weather_timeout_v1"] = "weather_timeout_v1"
    error_code: Literal["provider_timeout"] = "provider_timeout"
    message: str = Field(
        default="受控天气服务超时，当前没有可用预报。",
        min_length=1,
    )


class AlwaysTimeoutWeatherAdapter:
    location_input_language: Literal["any"] = "any"

    def __init__(self, fixture: WeatherTimeoutFixture | None = None) -> None:
        self.fixture = fixture or WeatherTimeoutFixture()

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        return WeatherResult(
            success=False,
            location=request.location,
            query_used=request.location,
            forecast=[],
            summary=self.fixture.message,
            provider="eval:weather-timeout-v1",
            output_ref="eval://weather/provider_timeout",
            errors=[
                {
                    "code": self.fixture.error_code,
                    "message": self.fixture.message,
                    "recoverable": True,
                }
            ],
        )


class WeatherTimeoutEnvironment:
    """Real Chat Provider plus one simulated, always-failing weather tool."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter

    def describe(self) -> dict[str, object]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "weather_provider": "simulated:weather_timeout_v1",
            "allowed_tools": ["weather"],
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        specs = registry.list_specs()
        fixture_result = AlwaysTimeoutWeatherAdapter().lookup(
            WeatherRequest(location="上海")
        )
        error_codes = [
            str(error.get("code"))
            for error in fixture_result.errors
            if isinstance(error, dict)
        ]
        return environment_validation(
            {
                "isolated_tool_registry": assertion(
                    registry.sealed and registry.list() == ["weather"],
                    (f"sealed={registry.sealed}, registered_tools={registry.list()}"),
                ),
                "weather_timeout_fixture": assertion(
                    (
                        not fixture_result.success
                        and error_codes == ["provider_timeout"]
                        and fixture_result.provider == "eval:weather-timeout-v1"
                    ),
                    (
                        f"success={fixture_result.success}, "
                        f"error_codes={error_codes}, "
                        f"provider={fixture_result.provider}"
                    ),
                ),
                "stateless_boundary": assertion(
                    all(spec.category == "read" for spec in specs),
                    (
                        "tool_categories="
                        f"{[spec.category for spec in specs]}；"
                        "运行时使用 in-memory session/trace store。"
                    ),
                ),
            }
        )

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest,
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        self.validate().require_valid()
        resolved_request = UserRequest.model_validate(request)
        config = self.config or ProviderConfig.from_env()
        if self.chat_adapter is None:
            validate_real_chat_config(config)
        isolated = replace(
            config,
            mem0_base_url=None,
            conversation_history_backend="memory",
            langgraph_checkpointer_backend="none",
            durable_tasks_enabled=False,
            durable_task_worker_enabled=False,
        )
        registry = self._build_registry()
        runtime = AgentGraphRuntime(
            config=isolated,
            registry=registry,
            chat_adapter=self.chat_adapter,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        )
        try:
            state = runtime.run_state(
                resolved_request,
                trace_context=RuntimeTraceContext(
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                ),
            )
            events = runtime.trace_store.list_by_run(state.run_id)
        finally:
            runtime.close()
        evidence = RunEvidence(
            task_id=task.id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            terminal_status=state.status,
            response=(
                state.response.model_dump(mode="json")
                if state.response is not None
                else None
            ),
            available_tools=available_tools(state, events),
            tool_executions=tool_executions(events),
            validation_results=validation_results(events),
            initial_state={},
            final_state={},
            state_diff={
                "added": [],
                "modified": [],
                "deleted": [],
            },
            trace_event_names=[
                event.canonical_event
                for event in events
                if event.canonical_event is not None
            ],
            provider_result_kinds=provider_result_kinds(events),
        )
        return TaskExecution(evidence=evidence, trace_events=events)

    @staticmethod
    def _build_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(WeatherTool(adapter=AlwaysTimeoutWeatherAdapter()))
        registry.seal()
        return registry
