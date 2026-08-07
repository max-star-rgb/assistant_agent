"""Controlled lodging, geocoding and transit Environment for itinerary planning."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
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


OBSERVED_AT = datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc)
PLACES = {
    "杭州东站": "120.212010,30.290870",
    "灵隐寺": "120.101406,30.240293",
    "中国丝绸博物馆": "120.139527,30.225315",
}
HOTEL_LOCATIONS = {
    "湖滨慢行酒店": "120.163210,30.252420",
    "杭州东站商务酒店": "120.208400,30.286900",
}
ROUTES = {
    (PLACES["杭州东站"], HOTEL_LOCATIONS["湖滨慢行酒店"]): {
        "summary": "地铁直达后步行",
        "duration_minutes": 35,
        "walking_distance_meters": 520,
        "transfers": 0,
    },
    (HOTEL_LOCATIONS["湖滨慢行酒店"], PLACES["杭州东站"]): {
        "summary": "步行后地铁直达",
        "duration_minutes": 36,
        "walking_distance_meters": 540,
        "transfers": 0,
    },
    (PLACES["杭州东站"], HOTEL_LOCATIONS["杭州东站商务酒店"]): {
        "summary": "公交直达",
        "duration_minutes": 12,
        "walking_distance_meters": 280,
        "transfers": 0,
    },
    (HOTEL_LOCATIONS["杭州东站商务酒店"], PLACES["杭州东站"]): {
        "summary": "公交直达",
        "duration_minutes": 12,
        "walking_distance_meters": 280,
        "transfers": 0,
    },
    (HOTEL_LOCATIONS["湖滨慢行酒店"], PLACES["灵隐寺"]): {
        "summary": "地铁加景区接驳",
        "duration_minutes": 38,
        "walking_distance_meters": 600,
        "transfers": 1,
    },
    (HOTEL_LOCATIONS["湖滨慢行酒店"], PLACES["中国丝绸博物馆"]): {
        "summary": "公交直达",
        "duration_minutes": 24,
        "walking_distance_meters": 350,
        "transfers": 0,
    },
    (HOTEL_LOCATIONS["杭州东站商务酒店"], PLACES["灵隐寺"]): {
        "summary": "地铁换乘公交",
        "duration_minutes": 65,
        "walking_distance_meters": 900,
        "transfers": 2,
    },
    (HOTEL_LOCATIONS["杭州东站商务酒店"], PLACES["中国丝绸博物馆"]): {
        "summary": "地铁换乘公交",
        "duration_minutes": 52,
        "walking_distance_meters": 700,
        "transfers": 1,
    },
}


def _normalize_location(value: Any) -> str:
    """Normalize model-supplied coordinates without requiring trailing zeros."""

    text = str(value or "")
    try:
        longitude, latitude = (float(item.strip()) for item in text.split(",", 1))
    except (TypeError, ValueError):
        return text
    return f"{longitude:.6f},{latitude:.6f}"


class _ItineraryLodgingAdapter:
    provider = "eval:controlled-travel-itinerary-lodging-v1"

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        matches = (
            request.destination == "杭州"
            and request.check_in == date(2026, 10, 2)
            and request.check_out == date(2026, 10, 5)
            and request.adults == 3
            and request.rooms == 1
            and request.max_nightly_price == 900
        )
        offers = []
        if matches:
            offers = [
                LodgingOffer(
                    offer_id="eval-lakeside-slow",
                    property_name="湖滨慢行酒店",
                    nightly_price=820,
                    total_price=2460,
                    currency="CNY",
                    price_basis="nightly_estimate",
                    refundable=True,
                    source_ref="eval://travel-itinerary/lodging/lakeside-slow",
                    address="杭州市上城区湖滨路18号",
                    longitude=120.163210,
                    latitude=30.252420,
                    booking_url="https://example.test/hotel/lakeside-slow",
                ),
                LodgingOffer(
                    offer_id="eval-east-station-business",
                    property_name="杭州东站商务酒店",
                    nightly_price=620,
                    total_price=1860,
                    currency="CNY",
                    price_basis="nightly_estimate",
                    refundable=True,
                    source_ref="eval://travel-itinerary/lodging/east-station-business",
                    address="杭州市上城区东宁路66号",
                    longitude=120.208400,
                    latitude=30.286900,
                    booking_url="https://example.test/hotel/east-station-business",
                ),
            ]
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=offers,
            observed_at=OBSERVED_AT,
            output_ref="eval://travel-itinerary/lodging/search",
            provider_notice=(
                "总价按展示每晚价乘3晚估算；库存、税费和退改条件"
                "以跳转后的 OTA 页面为准。"
            ),
        )


class _ItineraryAmapRunner:
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
            geocodes = []
            if place in PLACES and tool_input.get("city") == "杭州":
                geocodes = [
                    {
                        "name": place,
                        "address": f"杭州市受控地址：{place}",
                        "location": PLACES[place],
                    }
                ]
            return ToolResult(
                tool_name=GEO_TOOL,
                success=True,
                data={"geocodes": geocodes},
                model_observation={
                    "status": "succeeded",
                    "geocodes": geocodes,
                    "source": "eval:controlled-itinerary-geocode-v1",
                },
                output_ref=f"eval://travel-itinerary/geocode/{place or 'empty'}",
            )
        if tool_name == "maps_direction_transit_integrated":
            key = (
                _normalize_location(tool_input.get("origin")),
                _normalize_location(tool_input.get("destination")),
            )
            route = (
                ROUTES.get(key)
                if tool_input.get("city") == "杭州"
                and tool_input.get("cityd") == "杭州"
                else None
            )
            routes = [route] if route is not None else []
            return ToolResult(
                tool_name=TRANSIT_TOOL,
                success=True,
                data={"routes": routes},
                model_observation={
                    "status": "succeeded",
                    "routes": routes,
                    "source": "eval:controlled-itinerary-transit-v1",
                },
                output_ref="eval://travel-itinerary/transit",
            )
        raise ValueError("unsupported controlled AMap tool")


class TravelItineraryPlanningEnvironment(ControlledTaskEnvironment):
    """Read-only fixtures for one complete multi-day itinerary decision."""

    dependency_label = "controlled:travel-itinerary-planning-v1"
    tool_catalog_label = "default_complete_registry_plus_itinerary_fixtures"

    def setup(self) -> None:
        self._lodging = _ItineraryLodgingAdapter()
        self._amap = _ItineraryAmapRunner()

    def required_successes(self) -> tuple[str, ...]:
        return ("load_skill", "lodging_search", GEO_TOOL, TRANSIT_TOOL)

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> dict[str, AssertionResult]:
        lodging = self._lodging.search(
            LodgingSearchRequest(
                destination="杭州",
                check_in=date(2026, 10, 2),
                check_out=date(2026, 10, 5),
                adults=3,
                rooms=1,
                max_nightly_price=900,
            )
        )
        route = self._amap.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_direction_transit_integrated",
            tool_input={
                "origin": HOTEL_LOCATIONS["湖滨慢行酒店"],
                "destination": PLACES["灵隐寺"],
                "city": "杭州",
                "cityd": "杭州",
            },
        )
        station_route = self._amap.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_direction_transit_integrated",
            tool_input={
                "origin": HOTEL_LOCATIONS["湖滨慢行酒店"],
                "destination": PLACES["杭州东站"],
                "city": "杭州",
                "cityd": "杭州",
            },
        )
        required = {"load_skill", "lodging_search", GEO_TOOL, TRANSIT_TOOL}
        return {
            "complete_itinerary_catalog": rule_assertion(
                required <= set(registry.list()),
                f"registered_tools={registry.list()}",
                label="完整目录包含 Skill、住宿、地理编码和公交工具",
            ),
            "controlled_lodging_and_route_fixtures": rule_assertion(
                lodging.success
                and [item.nightly_price for item in lodging.offers] == [820, 620]
                and route.success
                and route.data["routes"][0]["duration_minutes"] == 38
                and station_route.success
                and station_route.data["routes"][0]["duration_minutes"] == 36,
                (
                    f"offers={[item.model_dump(mode='json') for item in lodging.offers]}, "
                    f"route={route.data['routes']}, "
                    f"station_route={station_route.data['routes']}"
                ),
                label="受控酒店报价、景点和车站通勤证据完整",
            ),
            "isolated_readonly_boundary": rule_assertion(
                all(registry.get_spec(name).category == "read" for name in required),
                "writes=False, state=in-memory-per-run",
                label="旅行规划工具均只读且状态隔离",
            ),
        }

    def build_registry(self) -> ToolRegistry:
        return build_travel_registry(
            definitions=[maps_geo_definition(), maps_transit_definition()],
            runner=self._amap,
            replacements={
                "lodging_search": LodgingSearchTool(self._lodging),
            },
        )
