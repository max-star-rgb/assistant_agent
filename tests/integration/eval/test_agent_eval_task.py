"""Offline contracts for the task-centered Agent eval framework."""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import evals.agent.langfuse_backend as langfuse_backend
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.cli import _emit_progress, _langfuse_client, main
from evals.agent.contracts import (
    AssertionResult,
    JudgeVerdict,
    RunEvidence,
    ToolOutcomeExpectation,
)
from evals.agent.grading import (
    dimension,
    enforce_tool_outcome_expectations,
    environment_validation,
    grader_result,
    grade_task,
    judge_assertion,
    rule_assertion,
)
from evals.agent.langfuse_backend import (
    _evaluations,
    _run_experiment_preserving_evaluator_errors,
    publish_tasks,
    run_tasks,
)
from evals.agent.judge import (
    JUDGE_MAX_RETRIES_ENV,
    JUDGE_NETWORK_MODE_ENV,
    JUDGE_NETWORK_MODE_IPV4_DIRECT,
    JUDGE_TIMEOUT_ENV,
    JudgeProviderSettings,
    ProviderLLMJudge,
    _judge_http_client,
    create_provider_judge,
)
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
                        "天气服务超时，我现在无法确认上海明早是否适合跑步，"
                        "也无法根据天气给出确定的穿着或雨具建议。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _AlwaysPassJudge:
    def __init__(self) -> None:
        self.criterion_ids: list[str] = []

    def evaluate(self, *, criterion_id: str, **_: Any) -> JudgeVerdict:
        self.criterion_ids.append(criterion_id)
        return JudgeVerdict(passed=True, reason="离线 Judge 基准通过。")


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


class _JudgeChat:
    provider = "scripted"
    model = "judge"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text='{"passed": false, "reason": "证据不支持回答。"}',
        )


class _FakeJudgeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _FakeJudgeObservationContext:
    def __init__(self, observation: _FakeJudgeObservation) -> None:
        self.observation = observation

    def __enter__(self) -> _FakeJudgeObservation:
        return self.observation

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeJudgeLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.observations: list[_FakeJudgeObservation] = []

    def start_as_current_observation(
        self,
        **kwargs: Any,
    ) -> _FakeJudgeObservationContext:
        observation = _FakeJudgeObservation()
        self.calls.append(kwargs)
        self.observations.append(observation)
        return _FakeJudgeObservationContext(observation)


def test_assertions_require_explicit_rule_or_judge_provenance() -> None:
    rule = rule_assertion(
        True,
        "结构化事实满足。",
        label="结构化事实检查",
    )
    judged = judge_assertion(
        JudgeVerdict(passed=False, reason="工具结果没有支持回答中的事实。"),
        criterion_id="outcome_evidence_usage",
        label="工具结果理解与证据使用",
    )

    assert rule.evaluation_method == "rule"
    assert rule.criterion_id is None
    assert judged.evaluation_method == "judge"
    assert judged.criterion_id == "outcome_evidence_usage"

    with pytest.raises(ValidationError, match="judge assertion must declare"):
        AssertionResult(
            passed=True,
            label="缺失 criterion 的 Judge",
            reason="缺少 criterion。",
            evaluation_method="judge",
        )
    with pytest.raises(ValidationError, match="rule assertion cannot declare"):
        AssertionResult(
            passed=True,
            label="错误携带 criterion 的 Rule",
            reason="Rule 不应携带 criterion。",
            evaluation_method="rule",
            criterion_id="unexpected_criterion",
        )


def test_provider_llm_judge_receives_named_rubric() -> None:
    adapter = _JudgeChat()
    langfuse = _FakeJudgeLangfuse()
    progress: list[dict[str, object]] = []
    evidence = RunEvidence(
        task_id="weather_timeout_recovery",
        run_id="judge-contract-run",
        trace_id=TRACE_ID,
        terminal_status="completed",
    )

    verdict = ProviderLLMJudge(
        adapter,
        settings=JudgeProviderSettings(
            timeout_seconds=12.0,
            max_retries=0,
        ),
        langfuse=langfuse,
        progress=progress.append,
    ).evaluate(
        criterion_id="outcome_evidence_usage",
        rubric="只判断回答是否有工具证据支持。",
        evidence=evidence,
    )

    assert verdict == JudgeVerdict(passed=False, reason="证据不支持回答。")
    payload = json.loads(adapter.requests[0].user_query)
    assert payload["criterion_id"] == "outcome_evidence_usage"
    assert payload["rubric"] == "只判断回答是否有工具证据支持。"
    assert payload["evidence"]["task_id"] == "weather_timeout_recovery"
    assert (
        adapter.requests[0].response_format["json_schema"]["name"]
        == "agent_eval_judge_verdict"
    )
    assert langfuse.calls == [
        {
            "name": "judge.outcome_evidence_usage",
            "as_type": "evaluator",
            "input": {
                "criterion_id": "outcome_evidence_usage",
                "rubric": "只判断回答是否有工具证据支持。",
                "task_id": "weather_timeout_recovery",
                "run_id": "judge-contract-run",
            },
            "metadata": {
                "timeout_seconds": 12.0,
                "max_retries": 0,
                "network_mode": "ipv4_direct",
                "stream": False,
            },
        }
    ]
    assert langfuse.observations[0].updates == [
        {
            "output": {
                "passed": False,
                "reason": "证据不支持回答。",
            }
        }
    ]
    assert [event["event"] for event in progress] == [
        "agent_eval.judge.started",
        "agent_eval.judge.completed",
    ]
    assert progress[1]["passed"] is False
    assert isinstance(progress[1]["elapsed_ms"], int)


def test_judge_provider_uses_independent_timeout_retry_and_stream_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeOpenAIClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = _FakeOpenAIClient()

    def fake_openai(**kwargs: Any) -> _FakeOpenAIClient:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr("evals.agent.judge.OpenAI", fake_openai)
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        qwen_api_key="judge-key",
        qwen_chat_base_url="https://judge.example/v1",
        qwen_chat_model="judge-model",
        qwen_chat_enable_thinking=True,
        chat_stream=True,
        chat_timeout_seconds=75.0,
    )

    judge = create_provider_judge(
        config,
        env={
            JUDGE_TIMEOUT_ENV: "18",
            JUDGE_MAX_RETRIES_ENV: "0",
            JUDGE_NETWORK_MODE_ENV: "environment",
        },
    )

    assert captured == {
        "api_key": "judge-key",
        "base_url": "https://judge.example/v1",
        "timeout": 18.0,
        "max_retries": 0,
    }
    assert judge.settings == JudgeProviderSettings(
        timeout_seconds=18.0,
        max_retries=0,
        network_mode="environment",
    )
    assert judge.adapter.timeout_seconds == 18.0
    assert judge.adapter.stream is False
    assert judge.adapter.enable_thinking is False
    judge.close()
    assert fake_client.closed is True

    with pytest.raises(RuntimeError, match=JUDGE_TIMEOUT_ENV):
        JudgeProviderSettings.from_env({JUDGE_TIMEOUT_ENV: "0"})
    with pytest.raises(RuntimeError, match=JUDGE_MAX_RETRIES_ENV):
        JudgeProviderSettings.from_env({JUDGE_MAX_RETRIES_ENV: "-1"})
    with pytest.raises(RuntimeError, match=JUDGE_NETWORK_MODE_ENV):
        JudgeProviderSettings.from_env({JUDGE_NETWORK_MODE_ENV: "automatic"})


def test_judge_ipv4_direct_network_bypasses_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    transport = object()
    client = object()

    def fake_transport(**kwargs: Any) -> object:
        captured["transport"] = kwargs
        return transport

    def fake_client(**kwargs: Any) -> object:
        captured["client"] = kwargs
        return client

    monkeypatch.setattr("evals.agent.judge.httpx.HTTPTransport", fake_transport)
    monkeypatch.setattr("evals.agent.judge.httpx.Client", fake_client)

    settings = JudgeProviderSettings.from_env(
        {
            JUDGE_TIMEOUT_ENV: "18",
            JUDGE_MAX_RETRIES_ENV: "0",
        }
    )
    result = _judge_http_client(settings)

    assert settings.network_mode == JUDGE_NETWORK_MODE_IPV4_DIRECT
    assert result is client
    assert captured == {
        "transport": {"local_address": "0.0.0.0"},
        "client": {
            "transport": transport,
            "timeout": 18.0,
            "trust_env": False,
        },
    }


def test_eval_progress_uses_stderr_without_polluting_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _emit_progress(
        {
            "event": "agent_eval.judge.started",
            "criterion_id": "outcome_evidence_usage",
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "event": "agent_eval.judge.started",
        "criterion_id": "outcome_evidence_usage",
    }


def test_langfuse_cannot_hide_judge_infrastructure_failure() -> None:
    original = RuntimeError(
        "LLM judge Provider failed: provider_network_error"
    )

    def failing_evaluator(**_: Any) -> list[object]:
        raise original

    def swallowing_run_experiment(
        *,
        evaluators: list[Any],
        **_: Any,
    ) -> object:
        with pytest.raises(RuntimeError):
            evaluators[0](output={}, metadata={})
        return object()

    with pytest.raises(
        RuntimeError,
        match="LLM judge Provider failed: provider_network_error",
    ) as captured:
        _run_experiment_preserving_evaluator_errors(
            swallowing_run_experiment,
            evaluator=failing_evaluator,
        )

    assert captured.value.__cause__ is original


def test_langfuse_client_ignores_unsupported_socks_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_langfuse(**kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        captured["all_proxy_during_init"] = __import__("os").environ.get(
            "ALL_PROXY"
        )
        captured["https_proxy_during_init"] = __import__("os").environ.get(
            "HTTPS_PROXY"
        )
        return object()

    monkeypatch.setattr("evals.agent.cli.Langfuse", fake_langfuse)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7888")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")

    client = _langfuse_client()

    assert client is not None
    assert captured == {
        "kwargs": {
            "public_key": "pk-test",
            "secret_key": "sk-test",
            "host": "http://localhost:3000",
        },
        "all_proxy_during_init": None,
        "https_proxy_during_init": "http://127.0.0.1:8080",
    }
    assert __import__("os").environ["ALL_PROXY"] == (
        "socks://127.0.0.1:7888"
    )


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
    environment_validation = environment.validate()
    assert environment_validation.passed is True
    assert (
        environment_validation.schema_version
        == "agent_eval_environment_validation_v2"
    )
    assert set(environment_validation.checks) == {
        "full_tool_registry",
        "outcome_contract_matches_registry",
        "weather_timeout_fixture",
        "isolated_state_boundary",
    }
    expectations = environment.tool_outcome_expectations()
    expectations_by_name = {item.tool_name: item for item in expectations}
    assert len(expectations) > 1
    assert {"web_search", "web_fetch"}.isdisjoint(expectations_by_name)
    assert expectations_by_name["weather"] == (
        ToolOutcomeExpectation.must_fail_with(
            "weather",
            error_code="provider_timeout",
        )
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

    assert execution.evidence.terminal_status == "completed"
    assert "weather" in execution.evidence.available_tools
    assert {"web_search", "web_fetch"}.isdisjoint(
        execution.evidence.available_tools
    )
    assert len(execution.evidence.available_tools) > 1
    assert len(environment.chat_adapter.requests[0].tools) > 1
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

    judge = _AlwaysPassJudge()
    result = grade_task(
        task=task,
        evidence=execution.evidence,
        judge=judge,
    )
    assert result.passed is True
    assert result.reward == 1.0
    assert result.schema_version == "agent_eval_grader_result_v4"
    assert result.dimensions.tool_execution.passed is True
    assert result.dimensions.tool_use.passed is True
    assert result.dimensions.state.passed is True
    assert result.dimensions.response.passed is True
    assert judge.criterion_ids == ["outcome_evidence_usage"]
    assert set(result.dimensions.response.assertions) == {
        "response_generated",
        "outcome_evidence_usage",
    }
    assert (
        result.dimensions.response.assertions[
            "response_generated"
        ].evaluation_method
        == "rule"
    )
    assert (
        result.dimensions.response.assertions[
            "outcome_evidence_usage"
        ].evaluation_method
        == "judge"
    )
    scores = _evaluations(result)
    assert [score.name for score in scores] == [
        "agent_eval.reward",
        "agent_eval.dimension.tool_execution",
        "agent_eval.dimension.tool_use",
        "agent_eval.dimension.state",
        "agent_eval.dimension.response",
    ]
    assert [score.value for score in scores] == [
        1.0,
        True,
        True,
        True,
        True,
    ]
    assert not any("weather" in score.name for score in scores)
    assert scores[2].metadata == {
        "assertion.outcome_matches_environment.passed": True,
        "assertion.outcome_matches_environment.label": "工具结果符合受控环境预期",
        "assertion.outcome_matches_environment.method": "rule",
        "assertion.weather_called_once.passed": True,
        "assertion.weather_called_once.label": "天气工具调用次数符合策略",
        "assertion.weather_called_once.method": "rule",
        "assertion.weather_arguments_correct.passed": True,
        "assertion.weather_arguments_correct.label": "天气查询参数正确",
        "assertion.weather_arguments_correct.method": "rule",
    }
    assert scores[4].metadata == {
        "assertion.response_generated.passed": True,
        "assertion.response_generated.label": "已生成面向用户的回答",
        "assertion.response_generated.method": "rule",
        "assertion.outcome_evidence_usage.passed": True,
        "assertion.outcome_evidence_usage.label": "回答忠于工具证据",
        "assertion.outcome_evidence_usage.method": "judge",
        "assertion.outcome_evidence_usage.criterion_id": "outcome_evidence_usage",
    }
    assert all(
        len(json.dumps(value, ensure_ascii=False)) <= 200
        for score in (scores[2], scores[4])
        for value in score.metadata.values()
    )


def test_langfuse_comments_explain_failures_without_internal_ids() -> None:
    passed_dimension = dimension(
        {
            "completed": rule_assertion(
                True,
                "terminal_status=completed",
                label="Runtime 正常完成",
            )
        }
    )
    tool_use = dimension(
        {
            "outcome_evidence_usage": judge_assertion(
                JudgeVerdict(
                    passed=False,
                    reason="天气工具超时后，回答仍声称获得了具体预报。",
                ),
                criterion_id="outcome_evidence_usage",
                label="工具结果理解与证据使用",
            )
        }
    )
    response = dimension(
        {
            "answer_completeness": judge_assertion(
                JudgeVerdict(
                    passed=False,
                    reason="回答没有诚实说明天气未知。",
                ),
                criterion_id="answer_completeness",
                label="最终回答质量",
            )
        }
    )
    result = grader_result(
        tool_execution=passed_dimension,
        tool_use=tool_use,
        state=passed_dimension,
        response=response,
    )

    scores = _evaluations(result)

    assert scores[2].comment == (
        "未通过 1/1 项检查：\n"
        "- 工具结果理解与证据使用："
        "天气工具超时后，回答仍声称获得了具体预报。"
    )
    assert scores[4].comment == (
        "未通过 1/1 项检查：\n"
        "- 最终回答质量：回答没有诚实说明天气未知。"
    )
    assert scores[0].comment == (
        "评测未通过：\n"
        "- 工具使用：工具结果理解与证据使用："
        "天气工具超时后，回答仍声称获得了具体预报。\n"
        "- 最终回答：最终回答质量：回答没有诚实说明天气未知。"
    )
    assert "outcome_evidence_usage" not in scores[2].comment
    assert "answer_completeness" not in scores[4].comment


def test_langfuse_comments_name_successful_checks_and_dimensions() -> None:
    passed_dimension = dimension(
        {
            "completed": rule_assertion(
                True,
                "terminal_status=completed",
                label="Runtime 正常完成",
            ),
            "trace_complete": rule_assertion(
                True,
                "trace is complete",
                label="Trace 事件完整",
            ),
        }
    )
    result = grader_result(
        tool_execution=passed_dimension,
        tool_use=passed_dimension,
        state=passed_dimension,
        response=passed_dimension,
    )

    scores = _evaluations(result)

    assert scores[1].comment == (
        "全部检查通过（2/2）：\n"
        "- Runtime 正常完成\n"
        "- Trace 事件完整"
    )
    assert scores[0].comment == (
        "评测通过：4 个必要维度全部通过：\n"
        "- 工具执行\n"
        "- 工具使用\n"
        "- 状态变化\n"
        "- 最终回答"
    )
    assert "completed" not in scores[1].comment
    assert "trace_complete" not in scores[1].comment


def test_weather_grader_checks_weather_independently_of_other_tool_calls() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    evidence = RunEvidence(
        task_id="weather_timeout_recovery",
        run_id="run-weather-with-native-fallback",
        trace_id=TRACE_ID,
        terminal_status="completed",
        response={
            "message": (
                "本地天气服务超时；我通过模型的联网能力补充查询后，"
                "给出了明早跑步建议。"
            )
        },
        available_tools=["calendar_search", "weather"],
        tool_executions=[
            {
                "name": "weather",
                "input": {"location": "上海", "target_date": tomorrow},
                "status": "failed",
                "terminal_event": "tool.failed",
                "exposed": True,
                "error_code": "provider_timeout",
            },
            {
                "name": "calendar_search",
                "input": {"query": "tomorrow"},
                "status": "succeeded",
                "terminal_event": "tool.finished",
                "exposed": True,
            },
        ],
        validation_results=[
            {"tool_name": "weather", "status": "accepted"},
            {"tool_name": "calendar_search", "status": "accepted"},
        ],
        state_diff={"added": [], "modified": [], "deleted": []},
    )

    result = load_entrypoint(
        load_task("weather_timeout_recovery").grader
    )(evidence, _AlwaysPassJudge())

    assert result.dimensions.tool_use.assertions["weather_called_once"].passed is True
    assert (
        result.dimensions.tool_use.assertions["weather_arguments_correct"].passed
        is True
    )


def test_success_expected_but_timeout_forces_tool_use_to_fail() -> None:
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
    grader = load_entrypoint(task.grader)
    task_local_result = grader(execution.evidence, _AlwaysPassJudge())

    success_expectations = [
        (
            ToolOutcomeExpectation.must_succeed("weather")
            if item.tool_name == "weather"
            else item
        )
        for item in environment.tool_outcome_expectations(
            execution.evidence.available_tools
        )
    ]
    result = enforce_tool_outcome_expectations(
        task_local_result,
        evidence=execution.evidence,
        expectations=success_expectations,
    )

    assert result.dimensions.tool_execution.passed is True
    assert result.dimensions.tool_use.passed is False
    assert (
        result.dimensions.tool_use.assertions[
            "outcome_matches_environment"
        ].passed
        is False
    )
    assert result.reward == 0.0

    with pytest.raises(
        RuntimeError,
        match="outcome expectations do not cover the available tools",
    ):
        enforce_tool_outcome_expectations(
            task_local_result,
            evidence=execution.evidence,
            expectations=[],
        )


def test_calibration_catches_judge_and_trajectory_failures() -> None:
    task = load_task("weather_timeout_recovery")

    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [result.fixture_id for result in results] == [
        "correct_honest_recovery",
        "correct_native_search_fallback",
        "misattributes_failed_weather",
        "repeated_identical_call",
    ]
    assert all(result.matched for result in results)
    assert [result.actual_pass for result in results] == [True, True, False, False]
    assert results[0].dimensions == {
        "tool_execution": True,
        "tool_use": True,
        "state": True,
        "response": True,
    }
    assert results[0].expected_judge_passes == {
        "outcome_evidence_usage": True,
    }
    assert results[0].actual_judge_passes == results[0].expected_judge_passes
    assert results[1].dimensions == {
        "tool_execution": True,
        "tool_use": True,
        "state": True,
        "response": True,
    }
    assert results[2].dimensions["tool_use"] is True
    assert results[2].dimensions["response"] is False
    assert results[3].dimensions == {
        "tool_execution": True,
        "tool_use": False,
        "state": True,
        "response": True,
    }


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


def test_active_dataset_task_ids_excludes_archived_items() -> None:
    class _Dataset:
        items = [
            {
                "status": "ACTIVE",
                "input": {"task_id": "weather_timeout_recovery"},
                "metadata": {"task_id": "weather_timeout_recovery"},
            },
            {
                "status": "ARCHIVED",
                "input": {"task_id": "weather_live_outdoor_run"},
                "metadata": {"task_id": "weather_live_outdoor_run"},
            },
        ]

    class _DatasetClient:
        def get_dataset(self, name: str) -> _Dataset:
            assert name == "assistant-agent-regression"
            return _Dataset()

    assert langfuse_backend.active_dataset_task_ids(
        _DatasetClient(),  # type: ignore[arg-type]
        dataset_name="assistant-agent-regression",
    ) == ["weather_timeout_recovery"]


def test_active_dataset_task_ids_rejects_duplicate_task_mapping() -> None:
    duplicate_item = {
        "status": "ACTIVE",
        "input": {"task_id": "weather_timeout_recovery"},
        "metadata": {"task_id": "weather_timeout_recovery"},
    }

    class _DatasetClient:
        def get_dataset(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(items=[duplicate_item, duplicate_item])

    with pytest.raises(RuntimeError, match="duplicate task_id"):
        langfuse_backend.active_dataset_task_ids(
            _DatasetClient(),  # type: ignore[arg-type]
        )


def test_run_tasks_dataset_mode_excludes_archived_items() -> None:
    captured_item_ids: list[str] = []

    class _Dataset:
        items = [
            SimpleNamespace(
                id="active-item",
                status="ACTIVE",
                metadata={"task_id": "weather_timeout_recovery"},
            ),
            SimpleNamespace(
                id="archived-item",
                status="ARCHIVED",
                metadata={"task_id": "weather_timeout_recovery"},
            ),
        ]

        def run_experiment(self, **_: Any) -> object:
            captured_item_ids.extend(str(item.id) for item in self.items)
            return object()

    class _Client:
        def get_dataset(self, _: str) -> _Dataset:
            return _Dataset()

    run_tasks(
        _Client(),  # type: ignore[arg-type]
        [load_task("weather_timeout_recovery")],
        config=ProviderConfig(),
        judge=_AlwaysPassJudge(),
        active_only=True,
    )

    assert captured_item_ids == ["active-item"]


def test_cli_dataset_active_runs_selected_git_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Dataset:
        items = [
            {
                "status": "ACTIVE",
                "input": {"task_id": "weather_timeout_recovery"},
                "metadata": {"task_id": "weather_timeout_recovery"},
            },
            {
                "status": "ARCHIVED",
                "input": {"task_id": "weather_live_outdoor_run"},
                "metadata": {"task_id": "weather_live_outdoor_run"},
            },
        ]

    class _Client:
        def get_dataset(self, _: str) -> _Dataset:
            return _Dataset()

    class _Judge:
        def close(self) -> None:
            pass

    class _Observer:
        def close(self, *, timeout: float) -> bool:
            assert timeout == 10.0
            return True

    client = _Client()
    selected_task_ids: list[str] = []

    def fake_run_tasks(
        received_client: object,
        tasks: list[object],
        **_: Any,
    ) -> SimpleNamespace:
        assert received_client is client
        selected_task_ids.extend(str(task.id) for task in tasks)
        return SimpleNamespace(
            run_name="ui-dataset-active",
            dataset_run_url="http://langfuse.test/experiment",
        )

    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "real")
    monkeypatch.setattr("evals.agent.cli._langfuse_client", lambda: client)
    monkeypatch.setattr(
        "evals.agent.cli.ProviderConfig.from_env",
        lambda: ProviderConfig(),
    )
    monkeypatch.setattr(
        "evals.agent.cli.validate_real_chat_config",
        lambda _: None,
    )
    monkeypatch.setattr(
        "evals.agent.cli.create_provider_judge",
        lambda *_args, **_kwargs: _Judge(),
    )
    monkeypatch.setattr(
        "evals.agent.cli.create_required_trace_observer",
        lambda: _Observer(),
    )
    monkeypatch.setattr("evals.agent.cli.run_tasks", fake_run_tasks)
    monkeypatch.setattr(
        "evals.agent.cli.primary_rewards",
        lambda _: [1.0],
    )

    exit_code = main(
        [
            "--run",
            "--dataset-active",
            "--dataset-name",
            "assistant-agent-regression",
            "--allow-real-provider",
            "--no-env-file",
            "--run-name",
            "ui-dataset-active",
        ]
    )

    assert exit_code == 0
    assert selected_task_ids == ["weather_timeout_recovery"]


def test_cli_dataset_active_requires_confirmation_before_langfuse_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_accessed() -> object:
        raise AssertionError("Langfuse must not be accessed before confirmation.")

    monkeypatch.setattr(
        "evals.agent.cli._langfuse_client",
        fail_if_accessed,
    )

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--run",
                "--dataset-active",
                "--no-env-file",
            ]
        )

    assert exc_info.value.code == 2


def test_real_run_gate_rejects_the_default_mock_provider() -> None:
    try:
        validate_real_chat_config(ProviderConfig())
    except RuntimeError as exc:
        assert "MULTIMODAL_AGENT_PROVIDER_MODE=real" in str(exc)
    else:
        raise AssertionError("default mock Provider must not enter Agent eval")


def test_invalid_environment_is_an_infrastructure_failure() -> None:
    validation = environment_validation(
        {
            "fixture_contract": rule_assertion(
                False,
                "受控依赖未满足声明。",
                label="受控依赖配置",
            )
        }
    )

    with pytest.raises(
        RuntimeError,
        match="Environment validation failed[\\s\\S]*受控依赖配置",
    ):
        validation.require_valid()
