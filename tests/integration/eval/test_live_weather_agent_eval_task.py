"""Offline wiring checks for the live-weather Agent eval Task."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    WeatherForecast,
    WeatherRequest,
    WeatherResult,
)
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.contracts import JudgeVerdict, ToolOutcomeExpectation
from evals.agent.grading import grade_task
from evals.agent.loader import load_entrypoint, load_task


TRACE_ID = "abcdef0123456789abcdef0123456789"
PARENT_SPAN_ID = "abcdef0123456789"


class _SuccessfulWeatherAdapter:
    location_input_language = "en"

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        return WeatherResult(
            success=True,
            location=request.location,
            query_used=request.location,
            forecast=[
                WeatherForecast(
                    date=tomorrow,
                    condition="小雨",
                    temperature_c=18,
                    high_c=21,
                    low_c=16,
                    precipitation_chance=0.7,
                )
            ],
            summary="上海明天有小雨，18℃。",
            provider="eval:controlled-live-weather",
            output_ref="eval://weather/live-success",
        )


class _LiveWeatherChat:
    provider = "scripted"
    model = "live-weather"

    def __init__(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="weather-live-call",
                            name="weather",
                            arguments={
                                "location": "Shanghai",
                                "target_date": tomorrow,
                            },
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "上海明早约18℃并有小雨，跑步需注意湿滑；"
                        "建议穿轻薄防水外层并带便携雨具。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _RecordingPassJudge:
    def __init__(self) -> None:
        self.criterion_ids: list[str] = []

    def evaluate(self, *, criterion_id: str, **_: Any) -> JudgeVerdict:
        self.criterion_ids.append(criterion_id)
        return JudgeVerdict(passed=True, reason="回答忠于天气工具证据。")


def test_live_weather_task_runs_active_runtime_with_controlled_adapter() -> None:
    task = load_task("weather_live_outdoor_run")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_LiveWeatherChat(),
        weather_adapter=_SuccessfulWeatherAdapter(),
    )

    assert environment.validate().passed is True
    expectations = environment.tool_outcome_expectations()
    expectations_by_name = {item.tool_name: item for item in expectations}
    assert len(expectations) > 1
    assert expectations_by_name["weather"] == (
        ToolOutcomeExpectation.must_succeed("weather")
    )
    assert all(
        not item.required and item.expected_result == "success"
        for name, item in expectations_by_name.items()
        if name != "weather"
    )
    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert "weather" in execution.evidence.available_tools
    assert len(execution.evidence.available_tools) > 1
    assert len(environment.chat_adapter.requests[0].tools) > 1
    assert len(execution.evidence.tool_executions) == 1
    tool = execution.evidence.tool_executions[0]
    assert tool.terminal_event == "tool.finished"
    assert tool.error_code is None
    assert (
        tool.output["model_observation"]["forecast"][0]["condition"]
        == "小雨"
    )

    judge = _RecordingPassJudge()
    result = grade_task(task=task, evidence=execution.evidence, judge=judge)
    assert result.passed is True
    assert judge.criterion_ids == ["weather_answer_grounded"]
    assert result.dimensions.response.passed is True


def test_live_weather_calibration_separates_grounding_and_call_policy() -> None:
    task = load_task("weather_live_outdoor_run")

    results = run_calibration(task, load_labeled_calibration_judge(task))

    assert [item.actual_pass for item in results] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert all(item.matched for item in results)
    assert results[3].dimensions == {
        "tool_execution": True,
        "tool_use": True,
        "state": True,
        "response": False,
    }
    assert results[4].dimensions == {
        "tool_execution": True,
        "tool_use": False,
        "state": True,
        "response": True,
    }
