"""Same-contract AMap replacements used by retained Agent Missions."""

from __future__ import annotations

from assistant_agent.mcp.adapter import (
    MCPProxyTool,
    MCPToolAdapter,
    MCPToolDefinition,
    MCPToolRunner,
)
from assistant_agent.mcp.config import MCPToolAdapterConfig


AMAP_SERVER_NAME = "amap_maps"
POI_TOOL = "mcp.amap_maps.maps_text_search"
GEO_TOOL = "mcp.amap_maps.maps_geo"
TRANSIT_TOOL = "mcp.amap_maps.maps_direction_transit_integrated"


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


def controlled_amap_proxy_tool(
    definition: MCPToolDefinition,
    *,
    runner: MCPToolRunner,
) -> MCPProxyTool:
    """Build one same-contract AMap proxy for an exact eval replacement."""

    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name=AMAP_SERVER_NAME,
            allowed_tools=[definition.name],
            read_only_tools=[definition.name],
        ),
        runner=runner,
    )
    return adapter.proxy_tool_for_definition(definition)


def controlled_amap_replacement(
    production_tool: object,
    *,
    runner: MCPToolRunner,
) -> MCPProxyTool:
    """Preserve a discovered production MCP contract while replacing its runner."""

    if not isinstance(production_tool, MCPProxyTool):
        raise TypeError("AMap replacement requires a production MCPProxyTool")
    return production_tool.with_runner(runner)
