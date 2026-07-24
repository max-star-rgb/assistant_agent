"""Offline contract tests for the thin Langfuse Runtime task."""

from __future__ import annotations

from typing import Any

from evals.cases.langfuse.experiment import (
    AgentExperimentTask,
    load_dataset_seed,
    run_langfuse_agent_experiment,
    seed_langfuse_dataset,
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

    def on_trace_event(self, event: Any) -> None:
        self.events.append(event)

    def close(self, *, timeout: float) -> bool:
        return timeout > 0


def _seed_item(case_id: str) -> dict[str, Any]:
    seed = load_dataset_seed()
    item = next(item for item in seed.items if item.id == case_id)
    return {
        "id": item.id,
        "input": item.input,
        "expected_output": item.expected_output,
        "metadata": {**item.metadata, "case_id": item.id},
    }


def test_explicit_seed_uses_stable_native_dataset_ids() -> None:
    seed = load_dataset_seed()
    client = _FakeLangfuseClient()

    result = seed_langfuse_dataset(client, seed)

    assert result.dataset_name == "assistant-agent-closed-loop-v1"
    assert result.seed_hash.startswith("sha256:")
    assert result.item_ids == [
        "agent_v1_daily_simple_015_create_dentist_event",
        "agent_v1_daily_simple_017_polite_message_no_tool",
        "agent_v1_daily_simple_002_calendar_read_team_sync",
    ]
    assert client.datasets[0]["metadata"]["seed_hash"] == result.seed_hash
    assert client.items[0]["metadata"]["case_id"] == result.item_ids[0]


def test_runtime_task_returns_compact_code_evaluator_evidence() -> None:
    observer = _FakeTraceObserver()

    output = AgentExperimentTask(
        client=_FakeLangfuseClient(),
        trace_observer=observer,
    )(item=_seed_item("agent_v1_daily_simple_015_create_dentist_event"))

    assert output.schema_version == "agent_experiment_output_v1"
    assert output.trace_id == TRACE_ID
    assert output.terminal_status == "completed"
    assert output.state_diff["added"][0]["title"] == "洗牙"
    assert output.tool_executions[0]["name"] == "calendar_create"
    assert output.tool_executions[0]["status"] == "succeeded"
    assert output.validation_results[0]["status"] == "accepted"
    assert output.response is not None
    assert "静安牙科诊所" in output.response["message"]
    assert "trace.content" in output.trace_event_names

    root_span = build_text_otel_span_specs(observer.events)[0]
    assert root_span.trace_id == TRACE_ID
    assert root_span.parent_span_id == PARENT_SPAN_ID


def test_runtime_task_covers_no_tool_and_read_only_capabilities() -> None:
    cases = {
        "agent_v1_daily_simple_017_polite_message_no_tool": [],
        "agent_v1_daily_simple_002_calendar_read_team_sync": ["calendar_search"],
    }

    for case_id, expected_tools in cases.items():
        output = AgentExperimentTask(client=_FakeLangfuseClient())(
            item=_seed_item(case_id)
        )

        assert output.terminal_status == "completed"
        assert [
            execution["name"] for execution in output.tool_executions
        ] == expected_tools
        assert output.initial_state == output.final_state


def test_experiment_wires_task_without_project_evaluators() -> None:
    client = _FakeLangfuseClient()

    result = run_langfuse_agent_experiment(
        client,
        dataset_name="dataset-sentinel",
        experiment_name="experiment-sentinel",
        run_name="run-sentinel",
        trace_observer=_FakeTraceObserver(),
    )

    assert result is not None
    assert client.dataset.name == "dataset-sentinel"
    assert client.dataset.run_kwargs["name"] == "experiment-sentinel"
    assert client.dataset.run_kwargs["run_name"] == "run-sentinel"
    assert isinstance(client.dataset.run_kwargs["task"], AgentExperimentTask)
    assert "evaluators" not in client.dataset.run_kwargs
    assert "run_evaluators" not in client.dataset.run_kwargs
    assert (
        client.dataset.run_kwargs["metadata"]["evaluation_owner"]
        == "langfuse_code_evaluator"
    )
