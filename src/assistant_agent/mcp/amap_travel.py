"""Place-based travel tools composed from allowlisted official AMap MCP tools."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field, ValidationError, field_validator

from assistant_agent.mcp.amap_route_links import _build_route_url, _normalize_coordinate
from assistant_agent.native_agent.context import AssistantRunContext
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
    lodging_tool: BaseTool | None = None,
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
    if lodging_tool is not None and lodging_tool.name in interrupt_tools:
        raise ValueError("Travel lodging dependency must not require approval")

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
        start_name, end_name = (
            " ".join(place.name.replace("<", "").replace(">", "").split())[:240]
            for place in (start, end)
        )
        artifact = with_tool_delivery(artifact, text=(
            f"<detail>\n{mode}：{start_name} → {end_name}\n"
            f"打开高德地图导航 <link>{url}</link>\n</detail>"
        ))
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

    @tool("travel_hotel_nearby", response_format="content_and_artifact")
    async def travel_hotel_nearby(
        place: PlaceName,
        hotel_keyword: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S", description="酒店名称或品牌。")],
        runtime: ToolRuntime[AssistantRunContext],
        check_in: Annotated[date | None, Field(description="入住日期 YYYY-MM-DD；省略时使用调用当天的北京时间日期。")] = None,
        check_out: Annotated[date | None, Field(description="退房日期 YYYY-MM-DD；省略时为入住日期次日。")] = None,
        adults: Annotated[int, Field(ge=1, le=16)] = 1,
        rooms: Annotated[int, Field(ge=1, le=8)] = 1,
    ):
        """查询指定地点附近的指定品牌或名称酒店，返回报价和预订链接。

        先用高德首个候选解析地标和城市，再用已配置的酒店服务查询。
        默认今天入住、明天退房（北京时间）；不承诺固定半径，只读，不下单。
        """
        arrival = check_in or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        departure = check_out or arrival + timedelta(days=1)
        if departure <= arrival:
            raise ToolException("invalid_stay_dates: 退房日期必须晚于入住日期")
        center = await resolve(place)
        if not center.city:
            raise ToolException("place_city_missing: 酒店查询需要地标所在城市")
        if lodging_tool is None:
            raise ToolException("lodging_unavailable: 酒店查询服务未配置")
        response = await lodging_tool.ainvoke({
            "type": "tool_call", "id": str(uuid4()), "name": lodging_tool.name,
            "args": {
                "destination": center.city, "nearby_poi": center.name,
                "keywords": hotel_keyword, "check_in": arrival, "check_out": departure,
                "adults": adults, "rooms": rooms, "runtime": runtime,
            },
        })
        if not isinstance(response, ToolMessage):
            raise ToolException("lodging_response_invalid: 酒店查询未返回工具结果")
        if response.status == "error":
            raise ToolException(str(response.content))
        if not isinstance(response.artifact, dict):
            raise ToolException("lodging_response_invalid: 酒店查询缺少结构化结果")
        query = {
            "center": center.model_dump(), "hotel_keyword": hotel_keyword,
            "check_in": arrival.isoformat(), "check_out": departure.isoformat(),
        }
        context_content, _ = native_content_and_artifact(query, {})
        hotel_content = response.content if isinstance(response.content, list) else [
            {"type": "text", "text": response.content},
        ]
        return [*context_content, *hotel_content], {**response.artifact, **query}

    result = []
    for dependencies, wrapper in ((route_dependencies, travel_route), (nearby_dependencies, travel_nearby)):
        if dependencies <= tools.keys():
            result.append(configure_builtin_tool(wrapper, bounded_expected_errors=True))
    if lodging_tool is not None and place_dependencies <= tools.keys():
        result.append(configure_builtin_tool(travel_hotel_nearby, bounded_expected_errors=True))
    return result
