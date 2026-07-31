"""Controlled geocoding-to-transit-route Environment."""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import AssertionResult
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.travel_support import (
    AMAP_SERVER_NAME,
    GEO_TOOL,
    TRANSIT_TOOL,
    build_travel_registry,
    maps_geo_definition,
    maps_transit_definition,
)


GEOCODES = {
    "杭州东站": {
        "address": "杭州市上城区全福桥路2号",
        "location": "120.212010,30.290870",
    },
    "中国丝绸博物馆": {
        "address": "杭州市西湖区玉皇山路73-1号",
        "location": "120.139527,30.225315",
    },
}
ROUTE = {
    "summary": "地铁1号线换乘公交12路",
    "duration_minutes": 52,
    "walking_distance_meters": 780,
    "transfers": 1,
}


class _TransitRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME:
            raise ValueError("unsupported controlled AMap server")
        if tool_name == "maps_geo":
            place = str(tool_input.get("address") or "")
            geocodes = (
                [dict(name=place, **GEOCODES[place])]
                if place in GEOCODES and tool_input.get("city") == "杭州"
                else []
            )
            return ToolResult(
                tool_name=GEO_TOOL,
                success=True,
                data={"geocodes": geocodes},
                model_observation={
                    "status": "succeeded",
                    "geocodes": geocodes,
                    "source": "eval:controlled-amap-geocode-v1",
                },
                output_ref=f"eval://amap/geocode/{place or 'empty'}",
            )
        if tool_name == "maps_direction_transit_integrated":
            expected = {
                "origin": GEOCODES["杭州东站"]["location"],
                "destination": GEOCODES["中国丝绸博物馆"]["location"],
                "city": "杭州",
                "cityd": "杭州",
            }
            routes = [ROUTE] if tool_input == expected else []
            return ToolResult(
                tool_name=TRANSIT_TOOL,
                success=True,
                data={"routes": routes},
                model_observation={
                    "status": "succeeded",
                    "routes": routes,
                    "source": "eval:controlled-amap-transit-v1",
                },
                output_ref="eval://amap/transit/hangzhou-east-silk-museum",
            )
        raise ValueError("unsupported controlled AMap tool")


class TravelTransitRouteEnvironment(ControlledTaskEnvironment):
    """Read-only route fixture requiring geocoded endpoints."""

    dependency_label = "controlled:amap-geocode-transit-v1"
    tool_catalog_label = "default_complete_registry_plus_controlled_amap"

    def setup(self) -> None:
        self._runner = _TransitRunner()

    def required_successes(self) -> tuple[str, ...]:
        return (GEO_TOOL, TRANSIT_TOOL)

    def task_validation_checks(
        self, registry: ToolRegistry
    ) -> dict[str, AssertionResult]:
        origin = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_geo",
            tool_input={"address": "杭州东站", "city": "杭州"},
        )
        destination = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_geo",
            tool_input={"address": "中国丝绸博物馆", "city": "杭州"},
        )
        route = self._runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_direction_transit_integrated",
            tool_input={
                "origin": GEOCODES["杭州东站"]["location"],
                "destination": GEOCODES["中国丝绸博物馆"]["location"],
                "city": "杭州",
                "cityd": "杭州",
            },
        )
        return {
            "full_tool_registry": rule_assertion(
                {GEO_TOOL, TRANSIT_TOOL} <= set(registry.list())
                and {"web_search", "web_fetch"}.isdisjoint(registry.list()),
                f"registered_tools={registry.list()}",
                label="完整目录包含受控地理编码和公交工具",
            ),
            "controlled_route_fixture": rule_assertion(
                origin.success
                and destination.success
                and route.success
                and origin.data["geocodes"][0]["location"]
                == GEOCODES["杭州东站"]["location"]
                and destination.data["geocodes"][0]["location"]
                == GEOCODES["中国丝绸博物馆"]["location"]
                and route.data["routes"] == [ROUTE],
                f"geocodes={GEOCODES}, route={ROUTE}",
                label="受控坐标与公交路线完整",
            ),
            "isolated_state_boundary": rule_assertion(
                all(
                    registry.get_spec(name).category == "read"
                    for name in (GEO_TOOL, TRANSIT_TOOL)
                ),
                "writes=False, state=in-memory-per-run",
                label="地图工具只读且任务状态隔离",
            ),
        }

    def build_registry(self) -> ToolRegistry:
        return build_travel_registry(
            definitions=[
                maps_geo_definition(),
                maps_transit_definition(),
            ],
            runner=self._runner,
        )
