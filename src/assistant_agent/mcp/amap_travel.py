"""Place-based travel tools composed from allowlisted official AMap MCP tools."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import uuid4

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field, ValidationError, field_validator

from assistant_agent.mcp.amap_route_links import _build_route_url, _normalize_coordinate
from assistant_agent.tools.delivery import with_tool_delivery
from assistant_agent.tools.native_boundary import configure_builtin_tool, native_content_and_artifact


PlaceName = Annotated[str, Field(min_length=1, max_length=300, pattern=r"\S")]
TravelMode = Literal["驾车", "公交", "步行", "骑行"]


class ResolvedPlace(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    city: str = ""
    location: str

    @field_validator("location")
    @classmethod
    def valid_location(cls, value: str) -> str:
        normalized = _normalize_coordinate(value)
        if normalized is None:
            raise ValueError("place_coordinate_invalid")
        return normalized


def create_amap_travel_tools(
    tools: dict[str, BaseTool], *, interrupt_tools: set[str],
) -> list[BaseTool]:
    """Keep low-level tools private; expose only complete, read-only workflows."""
    modes = {
        "驾车": ("maps_direction_driving", "car"),
        "公交": ("maps_direction_transit_integrated", "bus"),
        "步行": ("maps_direction_walking", "walk"),
        "骑行": ("maps_bicycling", "ride"),
    }
    if "maps_bicycling" not in tools:
        modes["骑行"] = ("maps_direction_bicycling", "ride")
    place_dependencies = {"maps_text_search", "maps_search_detail"}
    route_dependencies = place_dependencies | {name for name, _ in modes.values()}
    nearby_dependencies = place_dependencies | {"maps_around_search"}
    if interrupt_tools & (route_dependencies | nearby_dependencies) & tools.keys():
        raise ValueError("AMap travel dependencies must not require approval")

    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await tools[name].ainvoke({
            "type": "tool_call", "id": str(uuid4()), "name": name, "args": args,
        })
        if not isinstance(result, ToolMessage) or result.status == "error":
            raise ToolException(f"amap_tool_failed: {name}")
        blocks = result.content if isinstance(result.content, list) else [result.content]
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else block
            if not isinstance(text, str):
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        raise ToolException(f"amap_response_invalid: {name}")

    async def resolve(query: str) -> ResolvedPlace:
        data = await call("maps_text_search", {"keywords": query})
        candidates = data.get("pois")
        if not isinstance(candidates, list) or not candidates:
            raise ToolException(f"place_not_found: {query}")
        first = candidates[0]
        if not isinstance(first, dict) or not isinstance(first.get("id"), str) or not first["id"]:
            raise ToolException("amap_response_invalid: first POI has no ID")
        detail = await call("maps_search_detail", {"id": first["id"]})
        if _normalize_coordinate(detail.get("location")) is None:
            raise ToolException(f"place_coordinate_invalid: {query}")
        try:
            return ResolvedPlace.model_validate(detail)
        except ValidationError as exc:
            raise ToolException(f"amap_place_invalid: {query}") from exc

    @tool("travel_route", response_format="content_and_artifact")
    async def travel_route(origin: PlaceName, destination: PlaceName, mode: TravelMode):
        """按起点、终点名称和出行方式查询路线。地点名称尽量包含城市。

        内部各选第一个搜索候选并解析坐标，无需传入经纬度或 POI ID；
        返回实际选中的地点、路线距离与耗时及导航链接。不得省略结果中的实际地点。
        """
        start = await resolve(origin)
        end = await resolve(destination)
        name, navigation_mode = modes[mode]
        args = {"origin": start.location, "destination": end.location}
        if mode == "公交":
            if not start.city or not end.city:
                raise ToolException("place_city_missing: 公交路线需要起终点城市")
            args.update(city=start.city, cityd=end.city)
        route = await call(name, args)
        # The official package uses data for cycling and route for the other modes.
        body = route.get("route", route.get("data", route))
        if not isinstance(body, dict) or not body.get("transits" if mode == "公交" else "paths"):
            raise ToolException("route_not_found: 所选地点之间未找到该方式的路线")
        url = _build_route_url(origin=start.location, destination=end.location, mode=navigation_mode)
        artifact = {
            "origin": start.model_dump(), "destination": end.model_dump(),
            "mode": mode, "route": body, "navigation_url": url,
        }
        artifact = with_tool_delivery(artifact, text=f"[打开高德地图导航]({url})")
        return native_content_and_artifact(artifact, artifact)

    @tool("travel_nearby", response_format="content_and_artifact")
    async def travel_nearby(
        place: PlaceName,
        keywords: Annotated[str, Field(max_length=200)] = "",
        radius: Annotated[int, Field(ge=1, le=50000)] = 1000,
    ):
        """查询某地点周边有什么。place 尽量包含城市；keywords 如咖啡馆，留空不限类别。

        内部选择第一个地点候选作为中心，radius 单位米，默认 1000 米。
        返回实际中心地点与周边地点列表；范围围绕所选坐标，不代表整个景区范围。
        """
        center = await resolve(place)
        data = await call("maps_around_search", {
            "location": center.location, "keywords": keywords, "radius": str(radius),
        })
        if not isinstance(data.get("pois"), list):
            raise ToolException("amap_response_invalid: missing nearby places")
        artifact = {"center": center.model_dump(), "keywords": keywords, "radius": radius,
                    "places": data["pois"]}
        return native_content_and_artifact(artifact, artifact)

    result = []
    for dependencies, wrapper in ((route_dependencies, travel_route), (nearby_dependencies, travel_nearby)):
        if dependencies <= tools.keys():
            result.append(configure_builtin_tool(wrapper, bounded_expected_errors=True))
    return result
