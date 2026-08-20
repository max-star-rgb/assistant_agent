"""Deterministic AMap route-planning links for official MCP results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from math import isfinite
from typing import Any
from urllib.parse import urlencode

from langchain_mcp_adapters.interceptors import (
    MCPToolCallRequest,
    MCPToolCallResult,
)
from mcp.types import CallToolResult, TextContent


_AMAP_SERVER_NAME = "amap_maps"
_AMAP_ROUTE_MODES = {
    "maps_direction_driving": "car",
    "maps_direction_transit_integrated": "bus",
    "maps_bicycling": "ride",
    "maps_direction_walking": "walk",
}


async def amap_route_link_interceptor(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    """Append a clickable route-planning link to successful AMap route results."""

    result = await handler(request)
    if (
        request.server_name != _AMAP_SERVER_NAME
        or request.name not in _AMAP_ROUTE_MODES
        or not isinstance(result, CallToolResult)
        or result.isError
    ):
        return result
    route_url = _build_route_url(
        origin=request.args.get("origin"),
        destination=request.args.get("destination"),
        mode=_AMAP_ROUTE_MODES[request.name],
    )
    if route_url is None:
        return result
    link = f"[在高德地图中查看路线规划]({route_url})"
    return result.model_copy(
        update={
            "content": [
                *result.content,
                TextContent(type="text", text=link),
            ]
        }
    )


def _build_route_url(
    *,
    origin: Any,
    destination: Any,
    mode: str,
) -> str | None:
    normalized_origin = _normalize_coordinate(origin)
    normalized_destination = _normalize_coordinate(destination)
    if normalized_origin is None or normalized_destination is None:
        return None
    query = urlencode(
        {
            "from": f"{normalized_origin},起点",
            "to": f"{normalized_destination},终点",
            "mode": mode,
            "src": "assistant_agent",
            "callnative": "1",
        }
    )
    return f"https://uri.amap.com/navigation?{query}"


def _normalize_coordinate(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = tuple(part.strip() for part in value.split(","))
    if len(parts) != 2 or not all(parts):
        return None
    try:
        longitude, latitude = (float(part) for part in parts)
    except ValueError:
        return None
    if (
        not isfinite(longitude)
        or not isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None
    return ",".join(parts)


__all__ = ["amap_route_link_interceptor"]
