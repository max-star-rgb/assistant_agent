"""Offline coverage for foundational AMap weather Agent eval Tasks."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.loader import load_entrypoint, load_suite, load_task


TRACE_ID = "abcdef0123456789abcdef0123456789"
PARENT_SPAN_ID = "abcdef0123456789"
AMAP_WEATHER_TOOL = "mcp.amap_maps.maps_weather"


class _ScriptedWeatherChat:
    provider = "scripted"
    model = "amap-weather-foundation"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results: Iterator[ChatResult] = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def _weather_call(city: str = "上海市") -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="amap-weather-foundation",
        finish_reason="tool_calls",
        tool_calls=[
            NativeToolCall(
                id="amap-weather-call",
                name=AMAP_WEATHER_TOOL,
                arguments={"city": city},
            )
        ],
    )


def _answer(message: str) -> ChatResult:
    return ChatResult(
        provider="scripted",
        model="amap-weather-foundation",
        finish_reason="stop",
        response_text=message,
    )


@pytest.mark.parametrize(
    ("task_id", "capability", "tags"),
    [
        (
            "amap_weather_forecast_date_grounding",
            "relative_date_weather_grounding",
            {"readonly", "weather", "amap", "forecast"},
        ),
        (
            "amap_weather_missing_city_clarification",
            "missing_city_clarification",
            {"readonly", "weather", "amap", "clarification"},
        ),
        (
            "amap_weather_provider_failure_recovery",
            "weather_provider_failure_recovery",
            {"readonly", "weather", "amap", "recovery"},
        ),
    ],
)
def test_amap_weather_task_declares_one_capability(
    task_id: str,
    capability: str,
    tags: set[str],
) -> None:
    task = load_task(task_id)

    assert task.capability == capability
    assert task.request.metadata == {}
    assert set(task.tags) == tags
    assert task.environment.startswith(f"evals.agent.tasks.{task_id}.")
    assert task.grader.startswith(f"evals.agent.tasks.{task_id}.")


def test_amap_weather_tasks_are_in_readonly_and_release_suites() -> None:
    weather_task_ids = {
        "amap_weather_forecast_date_grounding",
        "amap_weather_missing_city_clarification",
        "amap_weather_provider_failure_recovery",
    }

    assert weather_task_ids <= set(load_suite("readonly"))
    assert weather_task_ids <= set(load_suite("release"))


@pytest.mark.parametrize(
    ("task_id", "fixture_check", "required", "expected_result", "error_code"),
    [
        (
            "amap_weather_forecast_date_grounding",
            "controlled_forecast_fixture",
            True,
            "success",
            None,
        ),
        (
            "amap_weather_missing_city_clarification",
            "controlled_weather_fixture",
            False,
            "success",
            None,
        ),
        (
            "amap_weather_provider_failure_recovery",
            "controlled_timeout_fixture",
            True,
            "failure",
            "provider_timeout",
        ),
    ],
)
def test_amap_weather_environment_exposes_only_namespaced_weather(
    task_id: str,
    fixture_check: str,
    required: bool,
    expected_result: str,
    error_code: str | None,
) -> None:
    task = load_task(task_id)
    environment = load_entrypoint(task.environment)()

    validation = environment.validate()
    expectations = {
        item.tool_name: item
        for item in environment.tool_outcome_expectations()
    }
    description = environment.describe()

    assert validation.passed is True
    assert {
        "full_tool_registry",
        "outcome_contract_matches_registry",
        fixture_check,
        "isolated_state_boundary",
    } == set(validation.checks)
    assert AMAP_WEATHER_TOOL in expectations
    assert "weather" not in expectations
    assert expectations[AMAP_WEATHER_TOOL].required is required
    assert expectations[AMAP_WEATHER_TOOL].expected_result == expected_result
    assert expectations[AMAP_WEATHER_TOOL].error_code == error_code
    assert len(expectations) > 1
    assert description["writes"] is False
    assert description["state_reset"] == "per_task_run"


def test_amap_weather_forecast_selects_tomorrow_by_date() -> None:
    task = load_task("amap_weather_forecast_date_grounding")
    environment_type = load_entrypoint(task.environment)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    chat = _ScriptedWeatherChat(
        [
            _weather_call(),
            _answer(
                f"高德预报中{tomorrow}是明天：白天中雨、30℃，"
                "夜间小雨、25℃。下午步行不太理想，建议调整时间；"
                "如仍出行，穿轻薄速干衣并带雨具。该结果是日级预报，"
                "不能代表15点的精确小时天气。"
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
        AMAP_WEATHER_TOOL
    ]
    tool = execution.evidence.tool_executions[0]
    assert tool.input == {"city": "上海市"}
    assert tool.terminal_event == "tool.finished"
    forecasts = tool.output["model_observation"]["forecasts"]
    assert len(forecasts) == 3
    assert forecasts[1]["date"] == tomorrow
    assert forecasts[1]["dayweather"] == "中雨"
    assert forecasts[1]["nightweather"] == "小雨"
    assert forecasts[1]["daytemp"] == "30"
    _assert_readonly_evidence(execution.evidence)


def test_amap_weather_missing_city_clarifies_without_tool_call() -> None:
    task = load_task("amap_weather_missing_city_clarification")
    environment_type = load_entrypoint(task.environment)
    chat = _ScriptedWeatherChat(
        [
            _answer(
                "请先告诉我你目前所在的城市或区县；"
                "没有地点，我无法查询当地天气并给出户外建议。"
            )
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

    assert AMAP_WEATHER_TOOL in execution.evidence.available_tools
    assert "weather" not in execution.evidence.available_tools
    assert execution.evidence.tool_executions == []
    assert execution.evidence.validation_results == []
    _assert_readonly_evidence(execution.evidence)


def test_amap_weather_timeout_is_visible_to_final_answer() -> None:
    task = load_task("amap_weather_provider_failure_recovery")
    environment_type = load_entrypoint(task.environment)
    chat = _ScriptedWeatherChat(
        [
            _weather_call(),
            _answer(
                "高德天气查询刚才超时，目前没有可核实的上海明日预报。"
                "我不会据此编造具体温度或降雨；你可以稍后让我重试。"
                "出发前请再确认最新预报，并准备可增减衣物和便携雨具。"
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
        AMAP_WEATHER_TOOL
    ]
    tool = execution.evidence.tool_executions[0]
    assert tool.terminal_event == "tool.failed"
    assert tool.error_code == "provider_timeout"
    _assert_readonly_evidence(execution.evidence)


@pytest.mark.parametrize(
    (
        "task_id",
        "fixture_ids",
        "first_dimensions",
        "second_failure",
        "third_grounding",
    ),
    [
        (
            "amap_weather_forecast_date_grounding",
            [
                "selects_tomorrow_day_forecast",
                "uses_today_instead_of_tomorrow",
                "overstates_hourly_precision",
            ],
            {
                "tool_execution": True,
                "tool_semantics": True,
                "grounding": True,
                "response_quality": True,
            },
            "grounding",
            False,
        ),
        (
            "amap_weather_missing_city_clarification",
            [
                "asks_for_city_without_calling",
                "guesses_city_and_calls_weather",
                "asks_vague_followup",
            ],
            {
                "tool_execution": True,
                "tool_semantics": True,
                "grounding": True,
                "response_quality": True,
            },
            "response_quality",
            True,
        ),
        (
            "amap_weather_provider_failure_recovery",
            [
                "reports_timeout_without_fabrication",
                "fabricates_forecast_after_timeout",
                "omits_recovery_guidance",
            ],
            {
                "tool_execution": True,
                "tool_semantics": False,
                "grounding": True,
                "response_quality": True,
            },
            "grounding",
            True,
        ),
    ],
)
def test_amap_weather_calibration_separates_credible_failures(
    task_id: str,
    fixture_ids: list[str],
    first_dimensions: dict[str, bool],
    second_failure: str,
    third_grounding: bool,
) -> None:
    task = load_task(task_id)

    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == fixture_ids
    assert all(item.matched for item in results)
    assert results[0].dimensions == first_dimensions
    assert results[1].dimensions[second_failure] is False
    assert results[2].dimensions["grounding"] is third_grounding
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
