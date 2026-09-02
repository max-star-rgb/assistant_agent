"""Deterministic AMap navigation links for official MCP route results."""

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

from assistant_agent.tools.delivery import (
    DELIVERY_ARTIFACT_KEY,
    ToolDeliveryArtifact,
)


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
    """Append a clickable navigation link to successful AMap route results."""

    result = await handler(request)
    if (
        isinstance(result, CallToolResult)
        and result.structuredContent
        and DELIVERY_ARTIFACT_KEY in result.structuredContent
    ):
        structured_content = dict(result.structuredContent)
        structured_content.pop(DELIVERY_ARTIFACT_KEY)
        result = result.model_copy(update={"structuredContent": structured_content})
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
    link = f"[打开高德地图导航]({route_url})"
    structured_content = dict(result.structuredContent or {})
    structured_content[DELIVERY_ARTIFACT_KEY] = ToolDeliveryArtifact(
        text=link
    ).model_dump(mode="json")
    return result.model_copy(
        update={
            "content": [
                *result.content,
                TextContent(type="text", text=link),
            ],
            "structuredContent": structured_content,
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
