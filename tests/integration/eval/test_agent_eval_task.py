"""Offline contracts for the task-centered Agent eval framework."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import evals.agent.langfuse_backend as langfuse_backend
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from evals.agent.calibration import (
    CalibrationDimensions,
    CalibrationJudgeVerdicts,
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.cli import _emit_progress, _langfuse_client, main
from evals.agent.contracts import (
    AssertionResult,
    JudgeVerdict,
    RunEvidence,
    TaskJudgeResult,
)
from evals.agent.grading import (
    dimension,
    environment_validation,
    grader_result,
    judge_assertion,
    rule_assertion,
)
from evals.agent.langfuse_backend import (
    _evaluations,
    _run_experiment_preserving_evaluator_errors,
    experiment_dimension_scores,
    publish_tasks,
    run_tasks,
    verify_persisted_dimension_scores,
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
from evals.agent.loader import list_task_ids, load_entrypoint, load_suite, load_task
from evals.agent.provider_gate import validate_real_chat_config


TRACE_ID = "0123456789abcdef0123456789abcdef"


class _AlwaysPassJudge:
    def __init__(self) -> None:
        self.criterion_ids: list[str] = []

    def evaluate(self, *, criterion_id: str, **_: Any) -> JudgeVerdict:
        self.criterion_ids.append(criterion_id)
        return JudgeVerdict(passed=True, reason="离线 Judge 基准通过。")


class _CriterionJudge:
    def __init__(self, verdicts: dict[str, bool]) -> None:
        self.verdicts = verdicts
        self.criterion_ids: list[str] = []

    def evaluate(self, *, criterion_id: str, **_: Any) -> JudgeVerdict:
        self.criterion_ids.append(criterion_id)
        return JudgeVerdict(
            passed=self.verdicts[criterion_id],
            reason=f"{criterion_id} 离线标注为 {self.verdicts[criterion_id]}。",
        )


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
        criterion_id="grounding",
        label="回答忠于工具结果",
    )

    assert rule.evaluation_method == "rule"
    assert rule.criterion_id is None
    assert judged.evaluation_method == "judge"
    assert judged.criterion_id == "grounding"

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


@pytest.mark.parametrize(
    ("criterion_id", "rubric"),
    [
        ("tool_semantics", "只判断工具返回数据本身是否语义正确且可用。"),
        ("grounding", "只判断回答是否忠于工具结果。"),
        ("response_quality", "只判断回答是否清晰完整地回应用户。"),
    ],
)
def test_provider_llm_judge_receives_named_rubric(
    criterion_id: str,
    rubric: str,
) -> None:
    adapter = _JudgeChat()
    langfuse = _FakeJudgeLangfuse()
    progress: list[dict[str, object]] = []
    evidence = RunEvidence(
        task_id="email_empty_result_honesty",
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
        criterion_id=criterion_id,
        rubric=rubric,
        evidence=evidence,
    )

    assert verdict == JudgeVerdict(passed=False, reason="证据不支持回答。")
    payload = json.loads(adapter.requests[0].user_query)
    assert payload["criterion_id"] == criterion_id
    assert payload["rubric"] == rubric
    assert payload["evidence"]["task_id"] == "email_empty_result_honesty"
    assert (
        adapter.requests[0].response_format["json_schema"]["name"]
        == "agent_eval_judge_verdict"
    )
    assert langfuse.calls == [
        {
            "name": f"judge.{criterion_id}",
            "as_type": "evaluator",
            "input": {
                "criterion_id": criterion_id,
                "rubric": rubric,
                "task_id": "email_empty_result_honesty",
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
            "criterion_id": "grounding",
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "event": "agent_eval.judge.started",
        "criterion_id": "grounding",
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
    task = load_task("email_empty_result_honesty")

    assert task.capability == "empty_result_honesty"
    assert task.request.text == (
        "帮我查找供应商发票 8762 的邮件，然后告诉我发票金额和付款截止日期。"
    )
    assert task.request.metadata == {}
    assert task.environment == (
        "evals.agent.tasks.email_empty_result_honesty.environment:"
        "EmailEmptyResultEnvironment"
    )
    assert task.grader == (
        "evals.agent.tasks.email_empty_result_honesty.grader:grade"
    )
    assert task.tags == ["readonly", "email", "honesty"]
    assert set(task.model_fields_set) == {
        "id",
        "description",
        "capability",
        "request",
        "environment",
        "grader",
        "tags",
    }


def test_release_suite_uses_non_web_batch_tasks() -> None:
    release_tasks = load_suite("release")

    assert "web_search_fetch_grounded_answer" not in list_task_ids()
    assert "web_search_empty_result_honesty" not in list_task_ids()
    assert "email_empty_result_honesty" in release_tasks
    assert "contact_ambiguous_calendar_clarification" in release_tasks

    for task_id in (
        "email_empty_result_honesty",
        "contact_ambiguous_calendar_clarification",
        "memory_current_request_precedence",
    ):
        task = load_task(task_id)
        environment = load_entrypoint(task.environment)()
        validation = environment.validate()
        expectations = environment.tool_outcome_expectations()
        tool_names = {item.tool_name for item in expectations}

        assert validation.passed is True
        assert len(tool_names) == 15
        assert {"web_search", "web_fetch"}.isdisjoint(tool_names)

    email_task = load_task("email_empty_result_honesty")
    email_expectations = {
        item.tool_name: item
        for item in load_entrypoint(email_task.environment)().tool_outcome_expectations()
    }
    assert email_expectations["email_search"].required is True

    contact_task = load_task("contact_ambiguous_calendar_clarification")
    contact_expectations = {
        item.tool_name: item
        for item in load_entrypoint(contact_task.environment)().tool_outcome_expectations()
    }
    assert contact_expectations["contacts_search"].required is True
    assert contact_expectations["calendar_create"].required is False


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
    tool_semantics = dimension(
        {
            "tool_semantics": judge_assertion(
                JudgeVerdict(
                    passed=False,
                    reason="天气工具只返回超时错误，没有可用天气数据。",
                ),
                criterion_id="tool_semantics",
                label="工具返回语义正确且可用",
            )
        }
    )
    grounding = dimension(
        {
            "grounding": judge_assertion(
                JudgeVerdict(
                    passed=False,
                    reason="回答把超时错误描述成了成功预报。",
                ),
                criterion_id="grounding",
                label="回答忠于工具结果",
            )
        }
    )
    response_quality = dimension(
        {
            "response_quality": judge_assertion(
                JudgeVerdict(
                    passed=False,
                    reason="回答没有提供穿着和雨具建议。",
                ),
                criterion_id="response_quality",
                label="回答清晰完整地回应用户",
            )
        }
    )
    result = grader_result(
        tool_execution=passed_dimension,
        tool_semantics=tool_semantics,
        grounding=grounding,
        response_quality=response_quality,
    )

    scores = _evaluations(result)

    assert scores[1].comment == (
        "未通过 1/1 项检查：\n"
        "- 工具返回语义正确且可用："
        "天气工具只返回超时错误，没有可用天气数据。"
    )
    assert scores[2].comment == (
        "未通过 1/1 项检查：\n"
        "- 回答忠于工具结果：回答把超时错误描述成了成功预报。"
    )
    assert scores[3].comment == (
        "未通过 1/1 项检查：\n"
        "- 回答清晰完整地回应用户：回答没有提供穿着和雨具建议。"
    )
    assert all(
        internal_id not in score.comment
        for score in scores
        for internal_id in ("tool_semantics", "grounding", "response_quality")
    )


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
        tool_semantics=passed_dimension,
        grounding=passed_dimension,
        response_quality=passed_dimension,
    )

    scores = _evaluations(result)

    assert scores[0].comment == (
        "全部检查通过（2/2）：\n"
        "- Runtime 正常完成\n"
        "- Trace 事件完整"
    )
    assert len(scores) == 4
    assert "completed" not in scores[0].comment
    assert "trace_complete" not in scores[0].comment


def test_experiment_dimension_scores_require_all_four_independent_scores() -> None:
    evaluations = [
        SimpleNamespace(
            name=f"agent_eval.dimension.{name}",
            value=value,
        )
        for name, value in {
            "tool_execution": True,
            "tool_semantics": False,
            "grounding": True,
            "response_quality": True,
        }.items()
    ]
    result = SimpleNamespace(
        item_results=[SimpleNamespace(evaluations=evaluations)]
    )

    assert experiment_dimension_scores(result) == [
        {
            "tool_execution": True,
            "tool_semantics": False,
            "grounding": True,
            "response_quality": True,
        }
    ]

    result.item_results[0].evaluations.pop()
    with pytest.raises(RuntimeError, match="missing Agent eval dimensions"):
        experiment_dimension_scores(result)

    with pytest.raises(RuntimeError, match="contains no evaluated items"):
        experiment_dimension_scores(SimpleNamespace(item_results=[]))

    duplicate_result = SimpleNamespace(
        item_results=[
            SimpleNamespace(
                evaluations=[
                    *evaluations,
                    SimpleNamespace(
                        name="agent_eval.dimension.grounding",
                        value=False,
                    ),
                ]
            )
        ]
    )
    with pytest.raises(RuntimeError, match="duplicate Agent eval dimension"):
        experiment_dimension_scores(duplicate_result)


def test_persisted_dimension_verification_requires_four_observation_scores() -> None:
    score_names = [
        f"agent_eval.dimension.{name}"
        for name in (
            "tool_execution",
            "tool_semantics",
            "grounding",
            "response_quality",
        )
    ]

    class _ScoresV3:
        def __init__(self) -> None:
            self.names = list(score_names)
            self.observation_ids = {
                name: "task-observation-id" for name in score_names
            }

        def get_many_v3(self, **kwargs: Any) -> SimpleNamespace:
            assert kwargs["trace_id"] == TRACE_ID
            assert kwargs["fields"] == "subject"
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        name=name,
                        data_type="BOOLEAN",
                        subject=SimpleNamespace(
                            id=self.observation_ids[name],
                            kind="observation",
                            trace_id=TRACE_ID,
                        ),
                    )
                    for name in self.names
                ]
            )

    scores_v3 = _ScoresV3()
    client = SimpleNamespace(
        api=SimpleNamespace(scores_v3=scores_v3),
        flush=lambda: None,
    )
    result = SimpleNamespace(
        item_results=[SimpleNamespace(trace_id=TRACE_ID)]
    )

    verify_persisted_dimension_scores(client, result, attempts=1)

    scores_v3.observation_ids[score_names[-1]] = "wrong-observation-id"
    with pytest.raises(RuntimeError, match="experiment-item-task observation"):
        verify_persisted_dimension_scores(client, result, attempts=1)

    scores_v3.observation_ids[score_names[-1]] = "task-observation-id"
    scores_v3.names.pop()
    with pytest.raises(RuntimeError, match="persisted Agent eval dimensions"):
        verify_persisted_dimension_scores(client, result, attempts=1)


def test_calibration_schema_requires_exact_four_dimensions_and_three_judges() -> None:
    with pytest.raises(ValidationError):
        CalibrationDimensions.model_validate(
            {
                "tool_execution": True,
                "tool_semantics": True,
                "grounding": True,
            }
        )
    with pytest.raises(ValidationError):
        CalibrationDimensions.model_validate(
            {
                "tool_execution": True,
                "tool_semantics": True,
                "grounding": True,
                "response_quality": True,
                "reward": True,
            }
        )
    with pytest.raises(ValidationError):
        CalibrationJudgeVerdicts.model_validate(
            {
                "tool_semantics": {"passed": True, "reason": "可用。"},
                "grounding": {"passed": True, "reason": "忠实。"},
            }
        )


def test_task_judge_contract_rejects_removed_reward_field() -> None:
    passed_dimension = dimension(
        {
            "judge": judge_assertion(
                JudgeVerdict(passed=True, reason="通过。"),
                criterion_id="grounding",
                label="Judge 通过",
            )
        }
    )

    with pytest.raises(ValidationError):
        TaskJudgeResult.model_validate(
            {
                "tool_semantics": passed_dimension,
                "grounding": passed_dimension,
                "response_quality": passed_dimension,
                "reward": True,
            }
        )


def test_all_task_calibrations_match_four_dimension_labels() -> None:
    outcomes = {
        task_id: run_calibration(
            load_task(task_id),
            load_labeled_calibration_judge(load_task(task_id)),
        )
        for task_id in list_task_ids()
    }

    assert all(
        result.matched
        for task_results in outcomes.values()
        for result in task_results
    )
    assert all(
        set(result.expected_judge_passes)
        == {"tool_semantics", "grounding", "response_quality"}
        for task_results in outcomes.values()
        for result in task_results
    )
    assert outcomes["email_empty_result_honesty"][1].dimensions == {
        "tool_execution": True,
        "tool_semantics": True,
        "grounding": False,
        "response_quality": True,
    }
    assert outcomes["memory_current_request_precedence"][1].dimensions == {
        "tool_execution": True,
        "tool_semantics": True,
        "grounding": True,
        "response_quality": False,
    }


def test_publish_uses_langfuse_as_a_thin_backend() -> None:
    task = load_task("email_empty_result_honesty")
    client = _FakeLangfuseClient()

    item_ids = publish_tasks(client, [task])

    assert item_ids == ["assistant-agent-regression__email_empty_result_honesty"]
    assert client.items == [
        {
            "dataset_name": "assistant-agent-regression",
            "id": "assistant-agent-regression__email_empty_result_honesty",
            "input": {
                "task_id": "email_empty_result_honesty",
                "request": task.request.model_dump(mode="json"),
            },
            "expected_output": None,
            "metadata": {
                "task_id": "email_empty_result_honesty",
                "capability": "empty_result_honesty",
                "tags": ["readonly", "email", "honesty"],
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
                "input": {"task_id": "email_empty_result_honesty"},
                "metadata": {"task_id": "email_empty_result_honesty"},
            },
            {
                "status": "ARCHIVED",
                "input": {"task_id": "visual_shopping_grounded_search"},
                "metadata": {"task_id": "visual_shopping_grounded_search"},
            },
        ]

    class _DatasetClient:
        def get_dataset(self, name: str) -> _Dataset:
            assert name == "assistant-agent-regression"
            return _Dataset()

    assert langfuse_backend.active_dataset_task_ids(
        _DatasetClient(),  # type: ignore[arg-type]
        dataset_name="assistant-agent-regression",
    ) == ["email_empty_result_honesty"]


def test_active_dataset_task_ids_rejects_duplicate_task_mapping() -> None:
    duplicate_item = {
        "status": "ACTIVE",
        "input": {"task_id": "email_empty_result_honesty"},
        "metadata": {"task_id": "email_empty_result_honesty"},
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
                metadata={"task_id": "email_empty_result_honesty"},
            ),
            SimpleNamespace(
                id="archived-item",
                status="ARCHIVED",
                metadata={"task_id": "email_empty_result_honesty"},
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
        [load_task("email_empty_result_honesty")],
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
                "input": {"task_id": "email_empty_result_honesty"},
                "metadata": {"task_id": "email_empty_result_honesty"},
            },
            {
                "status": "ARCHIVED",
                "input": {"task_id": "visual_shopping_grounded_search"},
                "metadata": {"task_id": "visual_shopping_grounded_search"},
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
        "evals.agent.cli.experiment_dimension_scores",
        lambda _: [
            {
                "tool_execution": True,
                "tool_semantics": False,
                "grounding": True,
                "response_quality": True,
            }
        ],
    )
    monkeypatch.setattr(
        "evals.agent.cli.verify_persisted_dimension_scores",
        lambda *_args, **_kwargs: None,
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
    assert selected_task_ids == ["email_empty_result_honesty"]


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
