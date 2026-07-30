"""Controlled AMap POI Environment."""

from __future__ import annotations

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
    POI_TOOL,
    build_travel_registry,
    maps_text_search_definition,
)


POIS = [
    {
        "id": "B0FFGZ4R7R",
        "name": "中国丝绸博物馆",
        "type": "科教文化服务;博物馆",
        "address": "杭州市西湖区玉皇山路73-1号",
        "location": "120.139527,30.225315",
    },
    {
        "id": "EVAL-SILK-SHOP",
        "name": "丝博文创商店",
        "type": "购物服务;特色商业街",
        "address": "杭州市西湖区玉皇山路73号",
        "location": "120.140012,30.225002",
    },
]


class _PoiRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME or tool_name != "maps_text_search":
            raise ValueError("unsupported controlled AMap tool")
        matches = (
            POIS
            if (
                "中国丝绸博物馆" in str(tool_input.get("keywords") or "")
                and tool_input.get("city") == "杭州"
            )
            else []
        )
        return ToolResult(
            tool_name=POI_TOOL,
            success=True,
            data={"pois": matches, "count": len(matches)},
            model_observation={
                "status": "succeeded",
                "pois": matches,
                "count": len(matches),
                "source": "eval:controlled-amap-poi-v1",
            },
            output_ref="eval://amap/poi/silk-museum",
        )


class TravelCityPoiEnvironment:
    """Read-only city-scoped POI fixture with a credible nearby distractor."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._runner = _PoiRunner()

    def describe(self) -> dict[str, Any]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:amap-poi-v1",
            "tool_catalog": "default_complete_registry_plus_controlled_amap",
            "registered_tool_count": len(self._build_registry().list()),
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        fixture = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_text_search",
            tool_input={
                "keywords": "中国丝绸博物馆",
                "city": "杭州",
            },
        )
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and POI_TOOL in registry.list()
                    and {"web_search", "web_fetch"}.isdisjoint(
                        registry.list()
                    ),
                    f"registered_tools={registry.list()}",
                    label="完整目录包含受控高德 POI 工具",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations}
                    == set(registry.list()),
                    f"expectation_count={len(expectations)}",
                    label="工具结果预期覆盖注册表",
                ),
                "controlled_poi_fixture": rule_assertion(
                    fixture.success
                    and fixture.data.get("pois") == POIS
                    and POIS[0]["type"].endswith("博物馆")
                    and POIS[1]["type"].startswith("购物服务"),
                    f"poi_names={[item['name'] for item in POIS]}",
                    label="受控 POI 与消歧候选完整",
                ),
                "isolated_state_boundary": rule_assertion(
                    registry.get_spec(POI_TOOL).category == "read",
                    "writes=False, state=in-memory-per-run",
                    label="地图工具只读且任务状态隔离",
                ),
            }
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        if available_tools is not None:
            registry = _subset_registry(registry, available_tools, POI_TOOL)
        return outcome_expectations(
            registry,
            required_successes=(POI_TOOL,),
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
            definitions=[maps_text_search_definition()],
            runner=self._runner,
        )


def _subset_registry(
    registry: ToolRegistry,
    available_tools: list[str],
    required_tool: str,
) -> ToolRegistry:
    subset = ToolRegistry()
    for name in dict.fromkeys([*available_tools, required_tool]):
        subset.register(
            registry.get(name),
            registry.registration_record(name),
        )
    subset.seal()
    return subset
