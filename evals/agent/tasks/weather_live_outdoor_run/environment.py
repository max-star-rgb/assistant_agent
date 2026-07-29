"""Real Chat Provider plus the configured live weather MCP tool."""

from __future__ import annotations

from dataclasses import replace

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    WeatherAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.backend import (
    configured_calendar_weather_contacts_tools,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    WeatherTool,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    RunEvidence,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.evidence import (
    available_tools,
    provider_result_kinds,
    tool_executions,
    validation_results,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.provider_gate import validate_real_chat_config


class WeatherLiveEnvironment:
    """Run the active Agent with the normal catalog and configured live weather."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
        weather_adapter: WeatherAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self.weather_adapter = weather_adapter

    def describe(self) -> dict[str, object]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "weather_provider": "configured_real:mcp",
            "tool_catalog": "default_runtime_catalog_with_normal_visibility",
            "writes": False,
            "state_reset": "per_task_run",
            "live_calls": ["chat_provider", "weather_mcp"],
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        specs = registry.list_specs()
        expectations = self.tool_outcome_expectations()
        mapping_available, mapping_reason = self._weather_mapping_status()
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and "weather" in registry.list()
                    and len(registry.list()) > 1,
                    f"sealed={registry.sealed}, registered_tools={registry.list()}",
                    label="默认完整工具注册表已装配",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations} == set(registry.list()),
                    (
                        f"expectation_tools={[item.tool_name for item in expectations]}, "
                        f"registered_tools={registry.list()}"
                    ),
                    label="成功结果预期覆盖天气工具",
                ),
                "live_weather_mapping": rule_assertion(
                    mapping_available,
                    mapping_reason,
                    label="真实 weather MCP mapping 已配置",
                ),
                "isolated_state_boundary": rule_assertion(
                    any(
                        spec.name == "weather" and spec.category == "read"
                        for spec in specs
                    ),
                    f"tool_categories={[spec.category for spec in specs]}",
                    label="天气工具只读且任务状态隔离",
                ),
            }
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        tool_names = available_tools or self._build_registry().list()
        return [
            (
                ToolOutcomeExpectation.must_succeed("weather")
                if name == "weather"
                else ToolOutcomeExpectation(
                    tool_name=name,
                    required=False,
                    expected_result="success",
                )
            )
            for name in tool_names
        ]

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest,
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        config = self.config or ProviderConfig.from_env()
        self.config = config
        if self.chat_adapter is None:
            validate_real_chat_config(config)
        if self.weather_adapter is None and config.provider_mode != "real":
            raise RuntimeError(
                "Live weather Agent eval requires "
                "MULTIMODAL_AGENT_PROVIDER_MODE=real."
            )
        self.validate().require_valid()
        isolated = replace(
            config,
            mem0_base_url=None,
            conversation_history_backend="memory",
            langgraph_checkpointer_backend="none",
            durable_tasks_enabled=False,
            durable_task_worker_enabled=False,
        )
        runtime = AgentGraphRuntime(
            config=isolated,
            registry=self._build_registry(),
            chat_adapter=self.chat_adapter,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        )
        try:
            state = runtime.run_state(
                UserRequest.model_validate(request),
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
            state_diff={"added": [], "modified": [], "deleted": []},
            trace_event_names=[
                event.canonical_event
                for event in events
                if event.canonical_event is not None
            ],
            provider_result_kinds=provider_result_kinds(events),
        )
        return TaskExecution(evidence=evidence, trace_events=events)

    def _build_registry(self) -> ToolRegistry:
        adapter = self.weather_adapter
        config = self.config or ProviderConfig(provider_mode="mock")
        registry = create_default_registry(config)
        if adapter is None:
            return registry
        return _replace_weather_tool(registry, WeatherTool(adapter=adapter))

    def _weather_mapping_status(self) -> tuple[bool, str]:
        if self.weather_adapter is not None:
            return True, "weather_adapter=injected_controlled_adapter"
        if self.config is None:
            return True, "weather MCP mapping 在正式运行配置加载后验证。"
        server_configs = load_mcp_server_configs_from_env()
        configured = configured_calendar_weather_contacts_tools(server_configs)
        return (
            "weather" in configured,
            f"configured_personal_tools={sorted(configured)}",
        )


def _replace_weather_tool(
    source: ToolRegistry,
    weather_tool: WeatherTool,
) -> ToolRegistry:
    registry = ToolRegistry()
    for name in source.list():
        registry.register(
            weather_tool if name == "weather" else source.get(name),
            source.registration_record(name),
        )
    registry.seal(assembly_report=source.assembly_report)
    return registry
