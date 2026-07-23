"""Offline contract tests for the Langfuse Experiment adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langfuse import Evaluation

from assistant_agent.eval.contracts import AgentEvalEvidence
from assistant_agent.eval.langfuse_experiment import (
    CalendarExperimentTask,
    calendar_item_evaluators,
    calendar_run_evaluators,
    load_langfuse_dataset_source,
    run_langfuse_calendar_experiment,
    sync_langfuse_dataset,
)
from assistant_agent.services.otel_mapping import build_text_otel_span_specs


TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN_ID = "0123456789abcdef"


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.dataset = _FakeDataset()

    def create_dataset(self, **kwargs: Any) -> object:
        self.datasets.append(kwargs)
        return object()

    def create_dataset_item(self, **kwargs: Any) -> object:
        self.items.append(kwargs)
        return object()

    def get_dataset(self, name: str) -> "_FakeDataset":
        self.dataset.name = name
        return self.dataset

    def get_current_trace_id(self) -> str:
        return TRACE_ID

    def get_current_observation_id(self) -> str:
        return PARENT_SPAN_ID


class _FakeDataset:
    def __init__(self) -> None:
        self.name = ""
        self.run_kwargs: dict[str, Any] = {}

    def run_experiment(self, **kwargs: Any) -> object:
        self.run_kwargs = kwargs
        return object()


class _FakeTraceObserver:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.closed = False

    def on_trace_event(self, event: Any) -> None:
        self.events.append(event)

    def close(self, *, timeout: float) -> bool:
        self.closed = timeout > 0
        return self.closed


def _source_item() -> dict[str, Any]:
    source = load_langfuse_dataset_source()
    item = source.items[0]
    return {
        "id": item.id,
        "input": item.input,
        "expected_output": item.expected_output,
        "metadata": {
            **item.metadata,
            "case_id": item.id,
            "dataset_hash": source.content_hash(),
        },
    }


def _source_item_by_id(case_id: str) -> dict[str, Any]:
    source = load_langfuse_dataset_source()
    item = next(item for item in source.items if item.id == case_id)
    return {
        "id": item.id,
        "input": item.input,
        "expected_output": item.expected_output,
        "metadata": {
            **item.metadata,
            "case_id": item.id,
            "dataset_hash": source.content_hash(),
        },
    }


def test_dataset_source_sync_uses_stable_native_ids_and_hash() -> None:
    source = load_langfuse_dataset_source()
    client = _FakeLangfuseClient()

    result = sync_langfuse_dataset(client, source)

    assert result.dataset_name == source.dataset_name
    assert result.dataset_hash.startswith("sha256:")
    assert result.item_ids == [
        "daily_simple_015_create_dentist_event",
        "daily_simple_017_polite_message_no_tool",
        "daily_simple_002_calendar_read_team_sync",
    ]
    assert client.datasets[0]["metadata"]["dataset_hash"] == result.dataset_hash
    assert client.items[0]["id"] == result.item_ids[0]
    assert client.items[0]["metadata"]["case_id"] == result.item_ids[0]


def test_experiment_task_reuses_langfuse_trace_and_returns_runtime_evidence() -> None:
    client = _FakeLangfuseClient()
    observer = _FakeTraceObserver()

    evidence = CalendarExperimentTask(
        client=client,
        trace_observer=observer,
    )(item=_source_item())

    assert evidence.trace_id == TRACE_ID
    assert evidence.terminal_status == "completed"
    assert evidence.state_diff["added"][0]["title"] == "洗牙"
    run_started = next(
        event
        for event in evidence.trace_events
        if event.canonical_event == "run.started"
    )
    assert run_started.parent_span_id == PARENT_SPAN_ID
    root_span = build_text_otel_span_specs(evidence.trace_events)[0]
    assert root_span.trace_id == TRACE_ID
    assert root_span.parent_span_id == PARENT_SPAN_ID
    assert observer.events == evidence.trace_events


def test_experiment_task_runs_all_versioned_capabilities() -> None:
    expected = {
        "daily_simple_017_polite_message_no_tool": [],
        "daily_simple_002_calendar_read_team_sync": ["calendar_search"],
    }

    for case_id, tool_names in expected.items():
        item = _source_item_by_id(case_id)
        evidence = CalendarExperimentTask(client=_FakeLangfuseClient())(item=item)
        evaluations = [
            evaluator(
                output=evidence,
                expected_output=item["expected_output"],
                metadata=item["metadata"],
            )
            for evaluator in calendar_item_evaluators()
        ]

        assert evidence.terminal_status == "completed"
        assert [
            event.tool_name
            for event in evidence.trace_events
            if event.canonical_event == "tool.started"
        ] == tool_names
        assert next(
            evaluation
            for evaluation in evaluations
            if evaluation.name == "agent.strict_pass"
        ).value is True


def test_item_evaluators_return_native_langfuse_evaluations() -> None:
    item = _source_item()
    evidence = CalendarExperimentTask(client=_FakeLangfuseClient())(item=item)

    evaluations = [
        evaluator(
            input=item["input"],
            output=evidence,
            expected_output=item["expected_output"],
            metadata=item["metadata"],
        )
        for evaluator in calendar_item_evaluators()
    ]

    assert all(isinstance(evaluation, Evaluation) for evaluation in evaluations)
    assert {evaluation.name for evaluation in evaluations} == {
        "agent.strict_pass",
        "agent.goal_completion",
        "agent.tool_correctness",
        "agent.policy_compliance",
        "agent.state_integrity",
        "agent.response_grounding",
        "agent.tool_call_count",
        "agent.total_latency_ms",
    }
    strict = next(
        evaluation
        for evaluation in evaluations
        if evaluation.name == "agent.strict_pass"
    )
    assert strict.value is True
    assert strict.data_type == "BOOLEAN"


def test_run_evaluators_aggregate_item_scores() -> None:
    item_results = [
        SimpleNamespace(
            evaluations=[
                Evaluation(name="agent.strict_pass", value=value),
                Evaluation(name="agent.goal_completion", value=value),
                Evaluation(name="agent.policy_compliance", value=value),
                Evaluation(name="agent.state_integrity", value=value),
                Evaluation(name="agent.response_grounding", value=value),
                Evaluation(name="agent.tool_call_count", value=count),
                Evaluation(name="agent.total_latency_ms", value=latency),
            ]
        )
        for value, count, latency in (
            (1.0, 1, 100),
            (0.0, 2, 300),
        )
    ]

    evaluations = {
        evaluator(item_results=item_results).name: evaluator(
            item_results=item_results
        ).value
        for evaluator in calendar_run_evaluators()
    }

    assert evaluations == {
        "strict_pass_rate": 0.5,
        "goal_completion_mean": 0.5,
        "policy_violation_rate": 0.5,
        "state_integrity_failure_rate": 0.5,
        "response_grounding_mean": 0.5,
        "tool_call_count_mean": 1.5,
        "latency_p50": 300.0,
        "latency_p95": 300.0,
    }


def test_dataset_experiment_wires_task_item_and_run_evaluators() -> None:
    source = load_langfuse_dataset_source()
    client = _FakeLangfuseClient()

    result = run_langfuse_calendar_experiment(
        client,
        source,
        experiment_name="experiment-sentinel",
        run_name="run-sentinel",
        trace_observer=_FakeTraceObserver(),
    )

    assert result is not None
    assert client.dataset.name == source.dataset_name
    assert client.dataset.run_kwargs["name"] == "experiment-sentinel"
    assert client.dataset.run_kwargs["run_name"] == "run-sentinel"
    assert isinstance(client.dataset.run_kwargs["task"], CalendarExperimentTask)
    assert client.dataset.run_kwargs["task"].trace_observer is not None
    assert len(client.dataset.run_kwargs["evaluators"]) == 8
    assert len(client.dataset.run_kwargs["run_evaluators"]) == 8


def test_experiment_evaluator_accepts_serialized_task_output() -> None:
    item = _source_item()
    evidence = CalendarExperimentTask(client=_FakeLangfuseClient())(item=item)
    serialized = AgentEvalEvidence.model_validate_json(evidence.model_dump_json())
    strict_evaluator = calendar_item_evaluators()[0]

    evaluation = strict_evaluator(
        input=item["input"],
        output=serialized.model_dump(mode="json"),
        expected_output=item["expected_output"],
        metadata=item["metadata"],
    )

    assert evaluation.name == "agent.strict_pass"
    assert evaluation.value is True
