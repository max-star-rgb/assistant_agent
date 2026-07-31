"""Controlled constrained-lodging-search Environment."""

from __future__ import annotations

from datetime import date, datetime, timezone
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
from evals.agent.task_support import build_controlled_registry


OBSERVED_AT = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
INVENTORY = [
    {
        "offer_id": "eval-west-lake-quiet",
        "property_name": "西湖清居酒店",
        "nightly_price": 568.0,
        "distance_meters": 600,
        "address": "杭州市西湖区南山路192号",
        "review": "距中国丝绸博物馆约0.6公里",
        "booking_url": "https://example.test/hotel/west-lake-quiet",
    },
    {
        "offer_id": "eval-nanshan-art",
        "property_name": "南山艺舍",
        "nightly_price": 598.0,
        "distance_meters": 1100,
        "address": "杭州市西湖区玉皇山路42号",
        "review": "距中国丝绸博物馆约1.1公里",
        "booking_url": "https://example.test/hotel/nanshan-art",
    },
    {
        "offer_id": "eval-lakeside-select",
        "property_name": "湖滨精选酒店",
        "nightly_price": 528.0,
        "distance_meters": 1800,
        "address": "杭州市上城区南山路88号",
        "review": "距中国丝绸博物馆约1.8公里",
        "booking_url": "https://example.test/hotel/lakeside-select",
    },
    {
        "offer_id": "eval-premium",
        "property_name": "西子臻选酒店",
        "nightly_price": 680.0,
        "distance_meters": 900,
        "address": "杭州市西湖区虎跑路9号",
        "review": "距中国丝绸博物馆约0.9公里",
        "booking_url": "https://example.test/hotel/premium",
    },
]


class _ConstrainedLodgingAdapter:
    provider = "eval:controlled-lodging-v1"

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        nights = (request.check_out - request.check_in).days
        items = list(INVENTORY)
        if (
            request.destination != "杭州"
            or request.check_in != date(2026, 8, 14)
            or request.check_out != date(2026, 8, 17)
            or request.adults != 2
            or request.rooms != 1
            or request.nearby_poi != "中国丝绸博物馆"
        ):
            items = []
        if request.max_nightly_price is not None:
            items = [
                item
                for item in items
                if item["nightly_price"] <= request.max_nightly_price
            ]
        if request.sort == "distance_asc":
            items.sort(key=lambda item: item["distance_meters"])
        elif request.sort == "price_asc":
            items.sort(key=lambda item: item["nightly_price"])
        elif request.sort == "price_desc":
            items.sort(key=lambda item: item["nightly_price"], reverse=True)
        offers = [
            LodgingOffer(
                offer_id=str(item["offer_id"]),
                property_name=str(item["property_name"]),
                nightly_price=float(item["nightly_price"]),
                total_price=float(item["nightly_price"]) * nights,
                currency=request.currency,
                price_basis="nightly_estimate",
                refundable=None,
                source_ref=f"eval://lodging/{item['offer_id']}",
                address=str(item["address"]),
                review=str(item["review"]),
                booking_url=str(item["booking_url"]),
            )
            for item in items[: request.limit]
        ]
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=offers,
            observed_at=OBSERVED_AT,
            output_ref="eval://lodging/search/silk-museum",
            provider_notice=(
                "总价按展示每晚价乘入住晚数估算；税费、库存和退改条件"
                "以跳转后的 OTA 页面为准。"
            ),
        )


class TravelLodgingConstraintEnvironment(ControlledTaskEnvironment):
    """Read-only lodging fixture exposing estimate and budget semantics."""

    dependency_label = "controlled:lodging-constraints-v1"
    tool_catalog_label = "default_complete_registry_without_local_web_access"

    def setup(self) -> None:
        self._adapter = _ConstrainedLodgingAdapter()

    def required_successes(self) -> tuple[str, ...]:
        return ("lodging_search",)

    def task_validation_checks(
        self, registry: ToolRegistry
    ) -> dict[str, AssertionResult]:
        fixture = self._adapter.search(
            LodgingSearchRequest(
                destination="杭州",
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 17),
                adults=2,
                rooms=1,
                nearby_poi="中国丝绸博物馆",
                max_nightly_price=600,
                sort="distance_asc",
            )
        )
        return {
            "full_tool_registry": rule_assertion(
                "lodging_search" in registry.list()
                and {"web_search", "web_fetch"}.isdisjoint(registry.list()),
                f"registered_tools={registry.list()}",
                label="完整目录包含受控酒店搜索工具",
            ),
            "controlled_lodging_fixture": rule_assertion(
                fixture.success
                and [item.nightly_price for item in fixture.offers]
                == [568.0, 598.0, 528.0]
                and [item.total_price for item in fixture.offers]
                == [1704.0, 1794.0, 1584.0]
                and all(
                    item.price_basis == "nightly_estimate" for item in fixture.offers
                ),
                (f"offers={[item.model_dump(mode='json') for item in fixture.offers]}"),
                label="受控酒店候选与估算口径完整",
            ),
            "isolated_state_boundary": rule_assertion(
                registry.get_spec("lodging_search").category == "read",
                "writes=False, state=in-memory-per-run",
                label="酒店工具只读且任务状态隔离",
            ),
        }

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            replacements={
                "lodging_search": LodgingSearchTool(self._adapter),
            }
        )
