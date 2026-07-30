"""Controlled dependencies for the meeting-logistics Mission."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarCreateTool,
    CalendarSearchTool,
)
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.plugins.builtin.python_execution.tool import (
    PythonInterpreterTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    ToolOutcomeExpectation,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.task_support import outcome_expectations
from evals.agent.travel_support import (
    AMAP_SERVER_NAME,
    GEO_TOOL,
    POI_TOOL,
    TRANSIT_TOOL,
    build_travel_registry,
    maps_geo_definition,
    maps_text_search_definition,
    maps_transit_definition,
)


VENUE = {
    "id": "EVAL-QINGPU-WANDA-MALL",
    "name": "上海青浦万达茂",
    "type": "购物服务;商场",
    "address": "上海市青浦区淀山湖大道851号",
    "location": "121.082829,31.133327",
}
ORIGIN = {
    "name": "上海虹桥站",
    "address": "上海市闵行区虹桥交通枢纽",
    "location": "121.320081,31.193964",
}
ROUTE = {
    "summary": "上海虹桥站乘地铁17号线至淀山湖大道站，出站后步行到会场",
    "duration_minutes": 50,
    "walking_distance_meters": 350,
    "transfers": 0,
}

AVAILABLE_LODGING = (
    ("eval-qingpu-riverside", "青浦水岸酒店", 568.0, "距上海青浦万达茂约0.8公里"),
    ("eval-dianshan-select", "淀山湖精选酒店", 538.0, "距上海青浦万达茂约1.4公里"),
    ("eval-qingpu-new-city", "青浦新城酒店", 488.0, "距上海青浦万达茂约2.2公里"),
)
UNAVAILABLE_LODGING_NAME = "万达近邻酒店"

_MAP_SOURCE = "eval:controlled-meeting-maps-v1"
_LODGING_SOURCE = "eval:controlled-meeting-lodging-v1"
_OBSERVED_AT = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
_REQUIRED_TOOLS = (
    POI_TOOL,
    GEO_TOOL,
    TRANSIT_TOOL,
    "lodging_search",
    "calendar_create",
)
_PROVIDER_NOTICE = (
    "距上海青浦万达茂约0.3公里的“万达近邻酒店”在预算内但当前无房，"
    "未进入 offers；价格和库存以 OTA 为准。"
)


class _MeetingMapsRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if server_name != AMAP_SERVER_NAME:
            raise ValueError("unsupported controlled AMap server")

        if tool_name == "maps_text_search":
            keywords = str(tool_input.get("keywords") or "")
            pois = (
                [VENUE]
                if "上海青浦万达茂" in keywords
                and tool_input.get("city") == "上海"
                else []
            )
            data: dict[str, Any] = {"pois": pois, "count": len(pois)}
        elif tool_name == "maps_geo":
            geocodes = (
                [ORIGIN]
                if tool_input.get("address") == "上海虹桥站"
                and tool_input.get("city") == "上海"
                else []
            )
            data = {"geocodes": geocodes}
        elif tool_name == "maps_direction_transit_integrated":
            routes = (
                [ROUTE]
                if tool_input
                == {
                    "origin": ORIGIN["location"],
                    "destination": VENUE["location"],
                    "city": "上海",
                    "cityd": "上海",
                }
                else []
            )
            data = {"routes": routes}
        else:
            raise ValueError("unsupported controlled AMap tool")

        return ToolResult(
            tool_name=f"mcp.{AMAP_SERVER_NAME}.{tool_name}",
            success=True,
            data=data,
            model_observation={
                "status": "succeeded",
                **data,
                "source": _MAP_SOURCE,
            },
            output_ref=f"eval://meeting/maps/{tool_name}",
        )


class _MeetingLodgingAdapter:
    provider = _LODGING_SOURCE

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        matches_fixture = (
            request.destination == "上海"
            and request.check_in == date(2026, 9, 17)
            and request.check_out == date(2026, 9, 19)
            and request.adults == 8
            and request.rooms == 4
            and request.nearby_poi == "上海青浦万达茂"
            and request.max_nightly_price == 600
            and request.sort == "distance_asc"
        )
        offers = (
            [
                LodgingOffer(
                    offer_id=offer_id,
                    property_name=property_name,
                    nightly_price=nightly_price,
                    total_price=nightly_price * 2,
                    currency=request.currency,
                    price_basis="nightly_estimate",
                    refundable=None,
                    source_ref=f"eval://meeting/lodging/{offer_id}",
                    review=distance,
                    booking_url=f"https://example.test/{offer_id}",
                )
                for offer_id, property_name, nightly_price, distance
                in AVAILABLE_LODGING
            ]
            if matches_fixture
            else []
        )
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=offers,
            observed_at=_OBSERVED_AT,
            output_ref="eval://meeting/lodging/search",
            provider_notice=(
                _PROVIDER_NOTICE if matches_fixture else None
            ),
        )


class MeetingLogisticsEnvironment:
    """Frozen maps/lodging dependencies with an isolated local calendar."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._tempdir = TemporaryDirectory(
            prefix="agent-eval-meeting-logistics-"
        )
        self._root = Path(self._tempdir.name)
        self._calendar_adapter = LocalSQLiteCalendarAdapter(
            self._root / "calendar.sqlite3",
            namespace="eval-meeting-logistics-user",
        )
        self._maps_runner = _MeetingMapsRunner()
        self._lodging_adapter = _MeetingLodgingAdapter()

    def describe(self) -> dict[str, Any]:
        registry = self._build_registry()
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": (
                "controlled:meeting-maps-lodging-local-calendar-v1"
            ),
            "tool_catalog": (
                "default_complete_registry_plus_controlled_meeting_tools"
            ),
            "registered_tool_count": len(registry.list()),
            "writes": True,
            "state_reset": "temporary_sqlite_per_environment",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        venue = self._maps_runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_text_search",
            tool_input={"keywords": "上海青浦万达茂", "city": "上海"},
        )
        origin = self._maps_runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_geo",
            tool_input={"address": "上海虹桥站", "city": "上海"},
        )
        route = self._maps_runner.run_tool(
            server_name=AMAP_SERVER_NAME,
            tool_name="maps_direction_transit_integrated",
            tool_input={
                "origin": ORIGIN["location"],
                "destination": VENUE["location"],
                "city": "上海",
                "cityd": "上海",
            },
        )
        lodging = self._lodging_adapter.search(
            LodgingSearchRequest(
                destination="上海",
                check_in=date(2026, 9, 17),
                check_out=date(2026, 9, 19),
                adults=8,
                rooms=4,
                nearby_poi="上海青浦万达茂",
                max_nightly_price=600,
                sort="distance_asc",
            )
        )
        registered = set(registry.list())
        dangerous_tools = [
            spec.name
            for spec in registry.list_specs()
            if spec.category == "dangerous"
        ]
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and set(_REQUIRED_TOOLS).issubset(registered)
                    and {"web_search", "web_fetch"}.isdisjoint(registered),
                    f"registered_tools={registry.list()}",
                    label="完整目录包含会议物流目标工具",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations} == registered,
                    f"expectation_count={len(expectations)}",
                    label="工具结果预期覆盖注册表",
                ),
                "controlled_maps_fixture": rule_assertion(
                    venue.success
                    and venue.data == {"pois": [VENUE], "count": 1}
                    and origin.success
                    and origin.data == {"geocodes": [ORIGIN]}
                    and route.success
                    and route.data == {"routes": [ROUTE]},
                    f"venue={venue.data}, origin={origin.data}, route={route.data}",
                    label="受控会场与交通数据完整",
                ),
                "controlled_lodging_fixture": rule_assertion(
                    lodging.success
                    and [
                        (
                            offer.offer_id,
                            offer.property_name,
                            offer.nightly_price,
                            offer.review,
                        )
                        for offer in lodging.offers
                    ]
                    == list(AVAILABLE_LODGING)
                    and all(
                        offer.total_price == offer.nightly_price * 2
                        and offer.price_basis == "nightly_estimate"
                        and offer.refundable is None
                        for offer in lodging.offers
                    )
                    and UNAVAILABLE_LODGING_NAME
                    not in {
                        offer.property_name for offer in lodging.offers
                    }
                    and UNAVAILABLE_LODGING_NAME
                    in (lodging.provider_notice or ""),
                    (
                        "offers="
                        f"{[offer.model_dump(mode='json') for offer in lodging.offers]}, "
                        f"provider_notice={lodging.provider_notice}"
                    ),
                    label="最近无房与可订住宿候选一致",
                ),
                "isolated_calendar": rule_assertion(
                    self._root.is_dir()
                    and self._calendar_adapter.path.parent == self._root
                    and self._calendar_adapter.snapshot()["events"] == [],
                    (
                        f"calendar_root={self._root.name}, "
                        f"namespace={self._calendar_adapter.namespace}"
                    ),
                    label="SQLite 日历按运行隔离",
                ),
                "side_effect_boundary": rule_assertion(
                    registry.get_spec("calendar_create").category == "write"
                    and not dangerous_tools,
                    (
                        "calendar_create_category="
                        f"{registry.get_spec('calendar_create').category}, "
                        f"dangerous_tools={dangerous_tools}"
                    ),
                    label="邀请预订付款能力未暴露",
                ),
            }
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        if available_tools is not None:
            subset = ToolRegistry()
            for name in dict.fromkeys([*available_tools, *_REQUIRED_TOOLS]):
                subset.register(
                    registry.get(name),
                    registry.registration_record(name),
                )
            subset.seal()
            registry = subset
        return outcome_expectations(
            registry,
            required_successes=_REQUIRED_TOOLS,
        )

    def _build_registry(self) -> ToolRegistry:
        return build_travel_registry(
            definitions=[
                maps_text_search_definition(),
                maps_geo_definition(),
                maps_transit_definition(),
            ],
            runner=self._maps_runner,
            replacements={
                "lodging_search": LodgingSearchTool(self._lodging_adapter),
                "calendar_search": CalendarSearchTool(
                    self._calendar_adapter
                ),
                "calendar_create": CalendarCreateTool(
                    self._calendar_adapter
                ),
                "python_interpreter": PythonInterpreterTool(
                    require_enable_env=False
                ),
            },
        )
