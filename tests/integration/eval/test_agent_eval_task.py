"""Offline contracts for the task-centered Agent eval framework."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.langfuse_backend import _evaluations, publish_tasks
from evals.agent.loader import load_entrypoint, load_task
from evals.agent.provider_gate import validate_real_chat_config


TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN_ID = "0123456789abcdef"


class _WeatherRecoveryChat:
    provider = "scripted"
    model = "weather-timeout-recovery"

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
                            id="weather-timeout-call",
                            name="weather",
                            arguments={
                                "location": "上海",
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
                        "天气服务超时，我现在无法确认上海明早的真实天气。"
                        "请稍后重试或在出发前查看可靠天气来源；如果仍无法确认，"
                        "可分层穿衣并携带便携雨具，遇到雷雨或大风就取消户外跑。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _AlwaysPassJudge:
    def evaluate(self, **_: Any) -> Any:
        from evals.agent.contracts import SemanticVerdict

        return SemanticVerdict(passed=True, reason="离线语义基准通过。")


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []

    def create_dataset(self, **kwargs: Any) -> object:
        self.datasets.append(kwargs)
        return object()

    def create_dataset_item(self, **kwargs: Any) -> object:
        self.items.append(kwargs)
        return object()


def test_task_keeps_runtime_and_grading_out_of_dataset_fields() -> None:
    task = load_task("weather_timeout_recovery")

    assert task.capability == "tool_failure_recovery"
    assert task.request.text.startswith("我明早六点半")
    assert task.request.metadata == {}
    assert task.environment.endswith(":WeatherTimeoutEnvironment")
    assert task.grader.endswith(":grade")
    assert set(task.model_fields_set) == {
        "id",
        "description",
        "capability",
        "request",
        "environment",
        "grader",
        "tags",
    }


def test_weather_timeout_environment_runs_the_real_runtime_offline() -> None:
    task = load_task("weather_timeout_recovery")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_WeatherRecoveryChat(),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert execution.evidence.available_tools == ["weather"]
    assert len(execution.evidence.tool_executions) == 1
    tool = execution.evidence.tool_executions[0]
    assert tool.name == "weather"
    assert tool.terminal_event == "tool.failed"
    assert tool.error_code == "provider_timeout"
    assert execution.evidence.state_diff == {
        "added": [],
        "modified": [],
        "deleted": [],
    }

    grader = load_entrypoint(task.grader)
    result = grader(execution.evidence, _AlwaysPassJudge())
    assert result.passed is True
    assert result.reward == 1.0
    scores = _evaluations(result)
    assert scores[0].name == "agent_eval.reward"
    assert scores[0].value == 1.0
    assert all(score.name.startswith("agent_eval.check.") for score in scores[1:])


def test_calibration_catches_semantic_and_trajectory_failures() -> None:
    task = load_task("weather_timeout_recovery")

    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [result.fixture_id for result in results] == [
        "correct_honest_recovery",
        "invented_forecast",
        "repeated_identical_call",
    ]
    assert all(result.matched for result in results)
    assert [result.actual_pass for result in results] == [True, False, False]


def test_publish_uses_langfuse_as_a_thin_backend() -> None:
    task = load_task("weather_timeout_recovery")
    client = _FakeLangfuseClient()

    item_ids = publish_tasks(client, [task])

    assert item_ids == ["assistant-agent-regression__weather_timeout_recovery"]
    assert client.items == [
        {
            "dataset_name": "assistant-agent-regression",
            "id": "assistant-agent-regression__weather_timeout_recovery",
            "input": {
                "task_id": "weather_timeout_recovery",
                "request": task.request.model_dump(mode="json"),
            },
            "expected_output": None,
            "metadata": {
                "task_id": "weather_timeout_recovery",
                "capability": "tool_failure_recovery",
                "tags": ["readonly", "critical", "regression"],
            },
        }
    ]
    assert "environment" not in client.items[0]["metadata"]
    assert "grader" not in client.items[0]["metadata"]


def test_real_run_gate_rejects_the_default_mock_provider() -> None:
    try:
        validate_real_chat_config(ProviderConfig())
    except RuntimeError as exc:
        assert "MULTIMODAL_AGENT_PROVIDER_MODE=real" in str(exc)
    else:
        raise AssertionError("default mock Provider must not enter Agent eval")
