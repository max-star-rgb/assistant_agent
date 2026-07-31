"""Stable contracts for governed hotel recommendation results."""

from datetime import date, datetime, timezone

from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.registry import ToolRegistry


class _FourOfferAdapter:
    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        offers = [
            LodgingOffer(
                offer_id=f"hotel-{index}",
                property_name=f"候选酒店 {index}",
                nightly_price=500 + index,
                total_price=(500 + index) * 2,
                currency="CNY",
                refundable=None,
                source_ref=f"flyai://hotel/{index}",
                address=f"杭州市测试路 {index} 号",
                latitude=30.25 + index / 100,
                longitude=120.16 + index / 100,
                star="豪华型",
                score=4.5,
                review="靠近目的地",
                image_url=f"https://images.example.test/hotel-{index}.jpg",
                booking_url=f"https://hotels.example.test/hotel-{index}",
            )
            for index in range(1, 5)
        ]
        return LodgingSearchResult(
            success=True,
            provider="flyai",
            offers=offers[: request.limit],
            observed_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            output_ref="flyai://lodging/search",
        )


def test_lodging_request_exposes_recommendation_filters_but_hides_limit() -> None:
    request = LodgingSearchRequest(
        destination="杭州",
        check_in=date(2026, 8, 1),
        check_out=date(2026, 8, 3),
        keywords="安静",
        nearby_poi="西湖",
        hotel_types=["酒店"],
        star_ratings=[4, 5],
        bed_types=["大床房"],
        max_nightly_price=800,
        sort="rate_desc",
    )
    registry = ToolRegistry()
    registry.register(LodgingSearchTool(_FourOfferAdapter()))

    schema = registry.get_spec("lodging_search").input_schema

    assert request.nearby_poi == "西湖"
    assert request.star_ratings == [4, 5]
    assert request.max_nightly_price == 800
    assert "limit" not in schema["properties"]
    assert set(schema["required"]) == {"destination", "check_in", "check_out"}


def test_lodging_tool_projects_three_bookable_candidates_without_booking() -> None:
    result = LodgingSearchTool(_FourOfferAdapter()).run(
        {
            "destination": "杭州",
            "check_in": "2026-08-01",
            "check_out": "2026-08-03",
        }
    )

    assert result.success is True
    assert result.model_observation is not None
    assert result.model_observation["status"] == "succeeded"
    assert len(result.model_observation["offers"]) == 3
    assert result.model_observation["offers"][0]["booking_url"] == (
        "https://hotels.example.test/hotel-1"
    )
    assert result.model_observation["offers"][0]["refundable"] is None
    assert len(result.data["offers"]) == 4
