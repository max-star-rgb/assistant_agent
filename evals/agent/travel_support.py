"""Shared controlled AMap assembly for travel and weather Agent eval Tasks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from assistant_agent.mcp.adapter import (
    MCPProxyTool,
    MCPToolAdapter,
    MCPToolDefinition,
    MCPToolRunner,
)
from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolRegistrationRecord
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.task_support import build_controlled_base_registry


AMAP_SERVER_NAME = "amap_maps"
POI_TOOL = "mcp.amap_maps.maps_text_search"
GEO_TOOL = "mcp.amap_maps.maps_geo"
TRANSIT_TOOL = "mcp.amap_maps.maps_direction_transit_integrated"
WEATHER_TOOL = "mcp.amap_maps.maps_weather"
AMAP_TOOL_NAMES = frozenset(
    {
        "mcp.amap_maps.maps_geo",
        "mcp.amap_maps.maps_ip_location",
        "mcp.amap_maps.maps_weather",
        "mcp.amap_maps.maps_bicycling",
        "mcp.amap_maps.maps_direction_walking",
        "mcp.amap_maps.maps_direction_driving",
        "mcp.amap_maps.maps_direction_transit_integrated",
        "mcp.amap_maps.maps_text_search",
        "mcp.amap_maps.maps_around_search",
    }
)


def maps_text_search_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_text_search",
        description="关键词搜，根据用户传入关键词，搜索出相关的POI。",
        input_schema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "city": {
                    "type": "string",
                    "description": "查询城市",
                },
                "types": {
                    "type": "string",
                    "description": "POI类型",
                },
            },
            "required": ["keywords"],
        },
    )


def maps_geo_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_geo",
        description=(
            "将详细的结构化地址转换为经纬度坐标，支持地标和建筑物名称。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "待解析的结构化地址信息",
                },
                "city": {
                    "type": "string",
                    "description": "指定查询的城市",
                },
            },
            "required": ["address"],
        },
    )


def maps_transit_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_direction_transit_integrated",
        description=(
            "根据起终点经纬度和城市规划公交、地铁等公共交通路线。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点经度,纬度",
                },
                "destination": {
                    "type": "string",
                    "description": "终点经度,纬度",
                },
                "city": {
                    "type": "string",
                    "description": "起点城市",
                },
                "cityd": {
                    "type": "string",
                    "description": "终点城市",
                },
            },
            "required": ["origin", "destination", "city", "cityd"],
        },
    )


def maps_weather_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_weather",
        description="根据城市名称或行政区编码查询当前及短期天气预报。",
        input_schema={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称或行政区编码",
                },
            },
            "required": ["city"],
        },
    )


def maps_ip_location_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_ip_location",
        description="根据 IP 地址定位所在省份、城市和行政区编码。",
        input_schema={
            "type": "object",
            "properties": {
                "ip": {
                    "type": "string",
                    "description": "需要定位的 IP 地址；省略时由服务端识别来源 IP",
                },
            },
        },
    )


def maps_around_search_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="maps_around_search",
        description="根据中心点坐标搜索指定半径内的 POI。",
        input_schema={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "中心点经度,纬度",
                },
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "radius": {
                    "type": "string",
                    "description": "搜索半径，单位为米",
                },
            },
            "required": ["location"],
        },
    )


def _route_definition(
    *,
    name: str,
    description: str,
) -> MCPToolDefinition:
    return MCPToolDefinition(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点经度,纬度",
                },
                "destination": {
                    "type": "string",
                    "description": "终点经度,纬度",
                },
            },
            "required": ["origin", "destination"],
        },
    )


def all_amap_definitions() -> tuple[MCPToolDefinition, ...]:
    """Return the deployment allowlist as a stable eval catalog."""

    return (
        maps_geo_definition(),
        maps_ip_location_definition(),
        maps_weather_definition(),
        _route_definition(
            name="maps_bicycling",
            description="根据起终点经纬度规划骑行路线。",
        ),
        _route_definition(
            name="maps_direction_walking",
            description="根据起终点经纬度规划步行路线。",
        ),
        _route_definition(
            name="maps_direction_driving",
            description="根据起终点经纬度规划驾车路线。",
        ),
        maps_transit_definition(),
        maps_text_search_definition(),
        maps_around_search_definition(),
    )


class _FallbackAmapRunner:
    """Return deterministic successful empty results for optional AMap calls."""

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME:
            raise ValueError("unsupported controlled AMap server")
        if tool_name in {"maps_text_search", "maps_around_search"}:
            data: dict[str, Any] = {"pois": [], "count": 0}
        elif tool_name == "maps_geo":
            data = {"geocodes": []}
        elif tool_name == "maps_ip_location":
            data = {
                "province": "",
                "city": "",
                "adcode": "",
                "rectangle": "",
            }
        elif tool_name == "maps_weather":
            data = {
                "city": str(tool_input.get("city") or ""),
                "forecasts": [],
            }
        elif tool_name in {
            "maps_bicycling",
            "maps_direction_walking",
            "maps_direction_driving",
            "maps_direction_transit_integrated",
        }:
            data = {"routes": []}
        else:
            raise ValueError("unsupported controlled AMap tool")
        return ToolResult(
            tool_name=f"mcp.{AMAP_SERVER_NAME}.{tool_name}",
            success=True,
            data=data,
            model_observation={
                "status": "succeeded",
                **data,
                "source": "eval:controlled-amap-default-v1",
            },
            output_ref=f"eval://amap/default/{tool_name}",
        )


class _RoutingAmapRunner:
    def __init__(
        self,
        *,
        target_runner: MCPToolRunner | None = None,
        target_tool_names: Iterable[str] = (),
    ) -> None:
        self._target_runner = target_runner
        self._target_tool_names = frozenset(target_tool_names)
        self._fallback = _FallbackAmapRunner()

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if self._target_runner is not None and tool_name in self._target_tool_names:
            return self._target_runner.run_tool(
                server_name=server_name,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        return self._fallback.run_tool(
            server_name=server_name,
            tool_name=tool_name,
            tool_input=tool_input,
        )


def add_controlled_amap_tools(
    base: ToolRegistry,
    *,
    runner: MCPToolRunner | None = None,
    target_tool_names: Iterable[str] = (),
) -> ToolRegistry:
    """Add every deployment-allowed AMap tool to an offline eval Registry."""

    registry = ToolRegistry()
    for name in base.list():
        registry.register(
            base.get(name),
            base.registration_record(name),
        )
    definitions = all_amap_definitions()
    remote_names = [item.name for item in definitions]
    config = MCPToolAdapterConfig(
        server_name=AMAP_SERVER_NAME,
        allowed_tools=remote_names,
        read_only_tools=remote_names,
    )
    adapter = MCPToolAdapter(
        config,
        runner=_RoutingAmapRunner(
            target_runner=runner,
            target_tool_names=target_tool_names,
        ),
    )
    for definition in definitions:
        tool: MCPProxyTool = adapter.proxy_tool_for_definition(definition)
        registry.register(
            tool,
            ToolRegistrationRecord(
                tool_name=tool.name,
                plugin_id=f"mcp.{AMAP_SERVER_NAME}",
                plugin_version="eval-controlled-v1",
                source_type="mcp",
                source_ref="eval:controlled-amap",
            ),
        )
    registry.seal()
    return registry


def build_travel_registry(
    *,
    definitions: Iterable[MCPToolDefinition],
    runner: MCPToolRunner,
    replacements: Mapping[str, Tool] | None = None,
) -> ToolRegistry:
    """Add the full AMap catalog, routing target tools to the Task runner."""

    target_definitions = list(definitions)
    base = build_controlled_base_registry(replacements=replacements)
    return add_controlled_amap_tools(
        base,
        runner=runner,
        target_tool_names=[item.name for item in target_definitions],
    )
