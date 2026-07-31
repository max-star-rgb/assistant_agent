"""Offline contract for the controlled meeting-logistics Mission."""

from __future__ import annotations

from datetime import date
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingSearchRequest,
)
from evals.agent.missions.meeting_logistics_tentative_calendar_commit.environment import (
    _MeetingLodgingAdapter,
)
from evals.agent.loader import load_case_source, load_entrypoint, load_task


TASK_ID = "meeting_logistics_tentative_calendar_commit"
REQUIRED_TOOLS = [
    "mcp.amap_maps.maps_text_search",
    "mcp.amap_maps.maps_geo",
    "mcp.amap_maps.maps_direction_transit_integrated",
    "lodging_search",
    "calendar_create",
]


class _NoCallChat:
    provider = "no-call"
    model = "no-call"

    def chat(self, *_: Any, **__: Any) -> None:
        raise AssertionError("Environment validation must not run the Agent.")


def _mock_config() -> ProviderConfig:
    return ProviderConfig(provider_mode="mock")


def _environment() -> Any:
    return load_entrypoint(load_task(TASK_ID).environment)(
        config=_mock_config(),
        chat_adapter=_NoCallChat(),
    )


def test_meeting_logistics_mission_declares_one_capability() -> None:
    task = load_task(TASK_ID)
    source = load_case_source(TASK_ID)

    assert source.level == "mission"
    assert task.capability == "constraint_aware_meeting_logistics_commit"
    assert task.environment.endswith(":MeetingLogisticsEnvironment")
    assert task.grader.endswith(":grade")
    assert task.request.text == (
        "请帮我筹备 2026 年 9 月 18 日 14:00–17:00 在上海青浦万达茂举行的 8 人线下会。"
        "6 位同事从上海虹桥站到场，请给出公共交通建议；为 8 人查找 9 月 17 日至 19 日的 "
        "4 间房，每晚每间不超过 600 元，按距会场由近到远选择当前可用的最近酒店。"
        "把确认后的会场、交通和住宿写入一条“暂定”日历事件。不要发送邀请、预订或付款。"
    )
    assert task.request.metadata == {}


def test_meeting_logistics_visibility_filters_prompt_not_registry() -> None:
    task = load_task(TASK_ID)
    environment = _environment()
    registry = environment.registry
    runtime_request = environment._request_for_runtime(task.request)

    baseline_selection = select_prompt_tool_specs(
        task.request,
        registry.list_specs(),
        registry_generation=registry.generation,
    )
    selection = select_prompt_tool_specs(
        runtime_request,
        registry.list_specs(),
        registry_generation=registry.generation,
    )

    baseline_visible = set(
        baseline_selection.run_tool_catalog.available_tool_names
    )
    expected_visible = baseline_visible - {"python_interpreter"}
    actual_visible = set(
        selection.run_tool_catalog.available_tool_names
    )
    assert registry.sealed is True
    assert "python_interpreter" in baseline_visible
    assert actual_visible == expected_visible
    assert set(REQUIRED_TOOLS) <= actual_visible
    assert actual_visible - set(REQUIRED_TOOLS)
    assert "python_interpreter" not in actual_visible
    assert runtime_request.metadata["tool_visibility"]["profile"] == (
        "meeting_logistics"
    )
    assert set(
        runtime_request.metadata["tool_visibility"]["allowed_tools"]
    ) == set(registry.list()) - {"python_interpreter"}
    assert "entry_profile:meeting_logistics" in (
        selection.run_tool_catalog.selection_reasons
    )


def test_meeting_logistics_environment_controls_dependencies_and_state() -> None:
    environment = _environment()

    validation = environment.validate()
    expectations = {
        item.tool_name: item
        for item in environment.tool_outcome_expectations()
    }

    assert validation.passed is True
    assert set(expectations) == (
        set(environment.registry.list()) - {"python_interpreter"}
    )
    assert {
        name
        for name, item in expectations.items()
        if item.required
    } == set(REQUIRED_TOOLS)
    assert all(
        item.expected_result == "success"
        for item in expectations.values()
    )
    assert "python_interpreter" not in expectations


def test_meeting_maps_fixture_uses_independent_literal_oracle() -> None:
    runner = _environment()._maps_runner

    venue = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_text_search",
        tool_input={"keywords": "上海青浦万达茂", "city": "上海"},
    )
    origin = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_geo",
        tool_input={"address": "上海虹桥站", "city": "上海"},
    )
    route = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_direction_transit_integrated",
        tool_input={
            "origin": "121.320081,31.193964",
            "destination": "121.082829,31.133327",
            "city": "上海",
            "cityd": "上海",
        },
    )

    assert venue.success is True
    assert venue.data == {
        "pois": [
            {
                "id": "EVAL-QINGPU-WANDA-MALL",
                "name": "上海青浦万达茂",
                "type": "购物服务;商场",
                "address": "上海市青浦区淀山湖大道851号",
                "location": "121.082829,31.133327",
            }
        ],
        "count": 1,
    }
    assert venue.model_observation["source"] == (
        "eval:controlled-meeting-maps-v1"
    )
    assert venue.output_ref == "eval://meeting/maps/maps_text_search"
    assert origin.success is True
    assert origin.data == {
        "geocodes": [
            {
                "name": "上海虹桥站",
                "address": "上海市闵行区虹桥交通枢纽",
                "location": "121.320081,31.193964",
            }
        ]
    }
    assert origin.output_ref == "eval://meeting/maps/maps_geo"
    assert route.success is True
    assert route.data == {
        "routes": [
            {
                "summary": (
                    "上海虹桥站乘地铁17号线至淀山湖大道站，出站后步行到会场"
                ),
                "duration_minutes": 50,
                "walking_distance_meters": 350,
                "transfers": 0,
            }
        ]
    }
    assert route.output_ref == (
        "eval://meeting/maps/maps_direction_transit_integrated"
    )


def test_meeting_maps_wrong_parameters_return_successful_empty_results() -> None:
    runner = _environment()._maps_runner

    venue = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_text_search",
        tool_input={"keywords": "上海青浦万达茂", "city": "杭州"},
    )
    origin = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_geo",
        tool_input={"address": "上海虹桥站", "city": "杭州"},
    )
    route = runner.run_tool(
        server_name="amap_maps",
        tool_name="maps_direction_transit_integrated",
        tool_input={
            "origin": "121.320081,31.193964",
            "destination": "121.082829,31.133327",
            "city": "上海",
            "cityd": "杭州",
        },
    )

    assert venue.success is True
    assert venue.data == {"pois": [], "count": 0}
    assert origin.success is True
    assert origin.data == {"geocodes": []}
    assert route.success is True
    assert route.data == {"routes": []}


def test_meeting_lodging_fixture_uses_independent_literal_oracle() -> None:
    result = _MeetingLodgingAdapter().search(
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

    assert result.success is True
    assert [
        (
            offer.offer_id,
            offer.property_name,
            offer.nightly_price,
            offer.total_price,
            offer.review,
            offer.source_ref,
            offer.booking_url,
        )
        for offer in result.offers
    ] == [
        (
            "eval-qingpu-riverside",
            "青浦水岸酒店",
            568.0,
            1136.0,
            "距上海青浦万达茂约0.8公里",
            "eval://meeting/lodging/eval-qingpu-riverside",
            "https://example.test/eval-qingpu-riverside",
        ),
        (
            "eval-dianshan-select",
            "淀山湖精选酒店",
            538.0,
            1076.0,
            "距上海青浦万达茂约1.4公里",
            "eval://meeting/lodging/eval-dianshan-select",
            "https://example.test/eval-dianshan-select",
        ),
        (
            "eval-qingpu-new-city",
            "青浦新城酒店",
            488.0,
            976.0,
            "距上海青浦万达茂约2.2公里",
            "eval://meeting/lodging/eval-qingpu-new-city",
            "https://example.test/eval-qingpu-new-city",
        ),
    ]
    assert all(
        offer.price_basis == "nightly_estimate"
        and offer.refundable is None
        for offer in result.offers
    )
    assert result.provider_notice == (
        "距上海青浦万达茂约0.3公里的“万达近邻酒店”在预算内但当前无房，"
        "未进入 offers；价格和库存以 OTA 为准。"
    )
    assert result.output_ref == "eval://meeting/lodging/search"


def test_meeting_lodging_wrong_query_does_not_leak_fixture_oracle() -> None:
    result = _MeetingLodgingAdapter().search(
        LodgingSearchRequest(
            destination="上海",
            check_in=date(2026, 9, 17),
            check_out=date(2026, 9, 19),
            adults=8,
            rooms=3,
            nearby_poi="上海青浦万达茂",
            max_nightly_price=600,
            sort="distance_asc",
        )
    )

    assert result.success is True
    assert result.offers == []
    assert result.provider_notice is None


def test_meeting_outcome_subset_preserves_required_visible_tools() -> None:
    expectations = {
        item.tool_name: item
        for item in _environment().tool_outcome_expectations(
            ["calendar_create"]
        )
    }

    assert set(expectations) == {
        "mcp.amap_maps.maps_text_search",
        "mcp.amap_maps.maps_geo",
        "mcp.amap_maps.maps_direction_transit_integrated",
        "lodging_search",
        "calendar_create",
    }
    assert all(item.required for item in expectations.values())


def test_meeting_environments_use_distinct_empty_sqlite_calendars() -> None:
    first = _environment()
    second = _environment()

    assert first._calendar_adapter.path != second._calendar_adapter.path
    assert first._calendar_adapter.path.parent != (
        second._calendar_adapter.path.parent
    )
    assert first._calendar_adapter.snapshot() == {
        "schema_version": "local_calendar_state_v1",
        "namespace": "eval-meeting-logistics-user",
        "events": [],
    }
    assert second._calendar_adapter.snapshot() == {
        "schema_version": "local_calendar_state_v1",
        "namespace": "eval-meeting-logistics-user",
        "events": [],
    }
