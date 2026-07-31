"""Offline coverage for foundational travel-planning Agent eval Tasks."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.loader import load_entrypoint, load_task


TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN_ID = "0123456789abcdef"
POI_TOOL = "mcp.amap_maps.maps_text_search"
GEO_TOOL = "mcp.amap_maps.maps_geo"
TRANSIT_TOOL = "mcp.amap_maps.maps_direction_transit_integrated"


class _ScriptedTravelChat:
    provider = "scripted"
    model = "travel-foundation"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results: Iterator[ChatResult] = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def _tool_result(
    *calls: tuple[str, str, dict[str, object]],
) -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="travel-foundation",
        finish_reason="tool_calls",
        tool_calls=[
            NativeToolCall(id=call_id, name=name, arguments=arguments)
            for call_id, name, arguments in calls
        ],
    )


def _answer(message: str) -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="travel-foundation",
        finish_reason="stop",
        response_text=message,
    )


@pytest.mark.parametrize(
    ("task_id", "capability", "target_tools", "tags"),
    [
        (
            "travel_city_poi_disambiguation",
            "city_scoped_poi_disambiguation",
            {POI_TOOL},
            {"readonly", "travel", "amap", "poi"},
        ),
        (
            "travel_transit_route_evidence_chain",
            "named_place_transit_evidence_chain",
            {GEO_TOOL, TRANSIT_TOOL},
            {"readonly", "travel", "amap", "multi-tool", "transit"},
        ),
        (
            "travel_lodging_constraint_grounding",
            "lodging_constraint_grounding",
            {"lodging_search"},
            {"readonly", "travel", "lodging", "budget"},
        ),
    ],
)
def test_travel_task_declares_one_foundational_capability(
    task_id: str,
    capability: str,
    target_tools: set[str],
    tags: set[str],
) -> None:
    task = load_task(task_id)
    environment = load_entrypoint(task.environment)()
    expectations = {
        item.tool_name: item for item in environment.tool_outcome_expectations()
    }

    assert task.capability == capability
    assert task.request.metadata == {}
    assert set(task.tags) == tags
    assert target_tools <= set(expectations)
    assert all(expectations[name].required for name in target_tools)
    assert all(expectations[name].expected_result == "success" for name in target_tools)


@pytest.mark.parametrize(
    ("task_id", "fixture_check"),
    [
        (
            "travel_city_poi_disambiguation",
            "controlled_poi_fixture",
        ),
        (
            "travel_transit_route_evidence_chain",
            "controlled_route_fixture",
        ),
        (
            "travel_lodging_constraint_grounding",
            "controlled_lodging_fixture",
        ),
    ],
)
def test_travel_environment_is_complete_controlled_and_readonly(
    task_id: str,
    fixture_check: str,
) -> None:
    task = load_task(task_id)
    environment = load_entrypoint(task.environment)()

    validation = environment.validate()
    description = environment.describe()
    expectations = environment.tool_outcome_expectations()

    assert validation.passed is True
    assert set(validation.checks) >= {
        "full_tool_registry",
        fixture_check,
        "isolated_state_boundary",
    }
    assert len(expectations) == description["registered_tool_count"]
    assert {"web_search", "web_fetch"}.isdisjoint(
        item.tool_name for item in expectations
    )
    assert description["writes"] is False
    assert description["state_reset"] == "per_task_run"


def test_city_poi_task_runs_active_runtime_offline() -> None:
    task = load_task("travel_city_poi_disambiguation")
    environment_type = load_entrypoint(task.environment)
    chat = _ScriptedTravelChat(
        [
            _tool_result(
                (
                    "search-silk-museum",
                    POI_TOOL,
                    {"keywords": "中国丝绸博物馆", "city": "杭州"},
                )
            ),
            _answer(
                "高德结果中，目标是中国丝绸博物馆，地址为杭州市西湖区"
                "玉皇山路73-1号，坐标120.139527,30.225315。"
                "同一结果里的丝博文创商店是商店，不是目标博物馆。"
            ),
        ]
    )
    environment = environment_type(
        config=_mock_config(),
        chat_adapter=chat,
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert [item.name for item in execution.evidence.tool_executions] == [POI_TOOL]
    observation = execution.evidence.tool_executions[0].output["model_observation"]
    assert [item["name"] for item in observation["pois"]] == [
        "中国丝绸博物馆",
        "丝博文创商店",
    ]
    _assert_readonly_evidence(execution.evidence)


def test_transit_task_runs_geocoding_before_route_offline() -> None:
    task = load_task("travel_transit_route_evidence_chain")
    environment_type = load_entrypoint(task.environment)
    chat = _ScriptedTravelChat(
        [
            _tool_result(
                (
                    "geo-hangzhou-east",
                    GEO_TOOL,
                    {"address": "杭州东站", "city": "杭州"},
                ),
                (
                    "geo-silk-museum",
                    GEO_TOOL,
                    {"address": "中国丝绸博物馆", "city": "杭州"},
                ),
            ),
            _tool_result(
                (
                    "route-to-museum",
                    TRANSIT_TOOL,
                    {
                        "origin": "120.212010,30.290870",
                        "destination": "120.139527,30.225315",
                        "city": "杭州",
                        "cityd": "杭州",
                    },
                )
            ),
            _answer(
                "从杭州东站到中国丝绸博物馆，推荐地铁1号线换乘公交12路，"
                "预计52分钟，步行约780米，换乘1次。坐标来自前两次地点解析。"
            ),
        ]
    )
    environment = environment_type(
        config=_mock_config(),
        chat_adapter=chat,
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert [item.name for item in execution.evidence.tool_executions] == [
        GEO_TOOL,
        GEO_TOOL,
        TRANSIT_TOOL,
    ]
    route = execution.evidence.tool_executions[-1]
    assert route.output["model_observation"]["routes"][0] == {
        "summary": "地铁1号线换乘公交12路",
        "duration_minutes": 52,
        "walking_distance_meters": 780,
        "transfers": 1,
    }
    _assert_readonly_evidence(execution.evidence)


def test_lodging_task_runs_constrained_search_offline() -> None:
    task = load_task("travel_lodging_constraint_grounding")
    environment_type = load_entrypoint(task.environment)
    chat = _ScriptedTravelChat(
        [
            _tool_result(
                (
                    "search-museum-hotels",
                    "lodging_search",
                    {
                        "destination": "杭州",
                        "check_in": "2026-08-14",
                        "check_out": "2026-08-17",
                        "adults": 2,
                        "rooms": 1,
                        "nearby_poi": "中国丝绸博物馆",
                        "max_nightly_price": 600,
                        "sort": "distance_asc",
                    },
                )
            ),
            _answer(
                "三晚候选：西湖清居酒店每晚568元、估算总价1704元；"
                "南山艺舍每晚598元、估算总价1794元；"
                "湖滨精选酒店每晚528元、估算总价1584元。"
                "都不超过每晚600元并按距离排序。"
                "这些总价按每晚展示价乘3晚估算，税费、库存和退改以OTA页面为准。"
            ),
        ]
    )
    environment = environment_type(
        config=_mock_config(),
        chat_adapter=chat,
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert [item.name for item in execution.evidence.tool_executions] == [
        "lodging_search"
    ]
    offers = execution.evidence.tool_executions[0].output["model_observation"]["offers"]
    assert [item["nightly_price"] for item in offers] == [568.0, 598.0, 528.0]
    assert [item["total_price"] for item in offers] == [
        1704.0,
        1794.0,
        1584.0,
    ]
    assert {item["price_basis"] for item in offers} == {"nightly_estimate"}
    _assert_readonly_evidence(execution.evidence)


@pytest.mark.parametrize(
    ("task_id", "tool_name", "arguments", "result_key"),
    [
        (
            "travel_city_poi_disambiguation",
            POI_TOOL,
            {"keywords": "中国丝绸博物馆", "city": "上海"},
            "pois",
        ),
        (
            "travel_transit_route_evidence_chain",
            TRANSIT_TOOL,
            {
                "origin": "120.000000,30.000000",
                "destination": "121.000000,31.000000",
                "city": "杭州",
                "cityd": "杭州",
            },
            "routes",
        ),
        (
            "travel_lodging_constraint_grounding",
            "lodging_search",
            {
                "destination": "杭州",
                "check_in": "2026-08-14",
                "check_out": "2026-08-17",
                "adults": 1,
                "rooms": 1,
                "nearby_poi": "中国丝绸博物馆",
                "max_nightly_price": 600,
                "sort": "distance_asc",
            },
            "offers",
        ),
    ],
)
def test_travel_environment_does_not_reward_wrong_constraints(
    task_id: str,
    tool_name: str,
    arguments: dict[str, object],
    result_key: str,
) -> None:
    task = load_task(task_id)
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=_mock_config(),
        chat_adapter=_ScriptedTravelChat(
            [
                _tool_result(("wrong-constraints", tool_name, arguments)),
                _answer("没有找到符合条件的结果。"),
            ]
        ),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    observation = execution.evidence.tool_executions[0].output["model_observation"]
    assert observation[result_key] == []


@pytest.mark.parametrize(
    ("task_id", "fixture_ids", "negative_dimension"),
    [
        (
            "travel_city_poi_disambiguation",
            [
                "selects_official_museum_poi",
                "confuses_shop_with_museum",
                "omits_disambiguation",
            ],
            "grounding",
        ),
        (
            "travel_transit_route_evidence_chain",
            [
                "geocodes_then_uses_transit_route",
                "skips_required_geocoding",
                "omits_route_tradeoffs",
            ],
            "tool_execution",
        ),
        (
            "travel_lodging_constraint_grounding",
            [
                "applies_constraints_and_explains_prices",
                "misstates_estimate_as_final_total",
                "omits_price_basis_caveat",
            ],
            "grounding",
        ),
    ],
)
def test_travel_calibration_separates_credible_failures(
    task_id: str,
    fixture_ids: list[str],
    negative_dimension: str,
) -> None:
    task = load_task(task_id)

    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == fixture_ids
    assert all(item.matched for item in results)
    assert all(results[0].dimensions.values())
    assert results[1].dimensions[negative_dimension] is False
    assert results[2].dimensions["grounding"] is True
    assert results[2].dimensions["response_quality"] is False


def _mock_config() -> ProviderConfig:
    return ProviderConfig(
        provider_mode="mock",
        langgraph_checkpointer_backend="none",
    )


def _assert_readonly_evidence(evidence: object) -> None:
    assert getattr(evidence, "initial_state") == {}
    assert getattr(evidence, "final_state") == {}
    assert getattr(evidence, "state_diff") == {
        "added": [],
        "modified": [],
        "deleted": [],
    }
