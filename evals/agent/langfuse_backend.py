"""Thin Langfuse Dataset and Experiment backend for Git-owned tasks."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from copy import copy
from dataclasses import replace
from typing import Any

from langfuse import Evaluation, Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    create_otlp_http_text_span_exporter,
)
from evals.agent.contracts import (
    GraderResult,
    RunEvidence,
    SemanticJudge,
    TaskEnvironment,
    TaskSpec,
)
from evals.agent.grading import DIMENSION_NAMES, grade_task
from evals.agent.loader import load_entrypoint, load_task


DEFAULT_DATASET_NAME = "assistant-agent-regression"
PRIMARY_REWARD_NAME = "agent_eval.reward"


def publish_tasks(
    client: Langfuse,
    tasks: Collection[TaskSpec],
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
) -> list[str]:
    client.create_dataset(
        name=dataset_name,
        description=(
            "Git-owned assistant_agent regression tasks; Langfuse stores "
            "Experiment traces and scores."
        ),
        metadata={"owner": "evals/agent", "kind": "regression"},
    )
    item_ids: list[str] = []
    for task in tasks:
        item_id = f"{dataset_name}__{task.id}"
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=item_id,
            input={
                "task_id": task.id,
                "request": task.request.model_dump(mode="json"),
            },
            expected_output=None,
            metadata={
                "task_id": task.id,
                "capability": task.capability,
                "tags": task.tags,
            },
        )
        item_ids.append(item_id)
    return item_ids


def run_tasks(
    client: Langfuse,
    tasks: Collection[TaskSpec],
    *,
    config: ProviderConfig,
    judge: SemanticJudge,
    dataset_name: str = DEFAULT_DATASET_NAME,
    run_name: str | None = None,
    trace_observer: TextOtelTraceObserver | None = None,
) -> Any:
    task_by_id = {task.id: task for task in tasks}
    dataset = client.get_dataset(dataset_name)
    selected_items = [
        item
        for item in dataset.items
        if isinstance(item.metadata, dict)
        and item.metadata.get("task_id") in task_by_id
    ]
    missing = set(task_by_id) - {
        str(item.metadata["task_id"])
        for item in selected_items
        if isinstance(item.metadata, dict)
    }
    if missing:
        raise RuntimeError(
            "Published Dataset is missing tasks: " + ", ".join(sorted(missing))
        )
    selected_dataset = copy(dataset)
    selected_dataset.items = selected_items

    def execute_item(*, item: Any, **_: Any) -> dict[str, Any]:
        item_input = _item_field(item, "input")
        if not isinstance(item_input, dict):
            raise RuntimeError("Dataset item input must be an object.")
        task_id = item_input.get("task_id")
        task = task_by_id.get(task_id)
        if task is None:
            raise RuntimeError(f"Unexpected Dataset task_id: {task_id!r}.")
        trace_id = client.get_current_trace_id()
        parent_span_id = client.get_current_observation_id()
        if not trace_id or not parent_span_id:
            raise RuntimeError("Langfuse Experiment trace context is unavailable.")
        environment_type = load_entrypoint(task.environment)
        environment: TaskEnvironment = environment_type(config=config)
        execution = environment.execute(
            task=task,
            request=item_input.get("request"),
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )
        if trace_observer is not None:
            for event in execution.trace_events:
                trace_observer.on_trace_event(event)
        return execution.evidence.model_dump(mode="json")

    def evaluate_item(
        *,
        output: Any,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> list[Evaluation]:
        task_id = (metadata or {}).get("task_id")
        if task_id not in task_by_id:
            raise RuntimeError(f"Evaluator received unknown task_id: {task_id!r}.")
        task = load_task(str(task_id))
        result: GraderResult = grade_task(
            task=task,
            evidence=RunEvidence.model_validate(output),
            judge=judge,
        )
        return _evaluations(result)

    return selected_dataset.run_experiment(
        name="assistant-agent-regression",
        run_name=run_name,
        description=(
            "Run Git-owned task environments through AgentGraphRuntime and "
            "score them with task-local graders."
        ),
        task=execute_item,
        evaluators=[evaluate_item],
        max_concurrency=1,
        metadata={
            "framework": "evals/agent",
            "task_count": len(task_by_id),
        },
    )


def primary_rewards(result: Any) -> list[float]:
    rewards: list[float] = []
    for item_result in result.item_results:
        match = next(
            (
                evaluation
                for evaluation in item_result.evaluations
                if evaluation.name == PRIMARY_REWARD_NAME
            ),
            None,
        )
        if match is None or not isinstance(match.value, (int, float)):
            raise RuntimeError("Experiment result is missing agent_eval.reward.")
        rewards.append(float(match.value))
    return rewards


def create_required_trace_observer(
    env: Mapping[str, str] | None = None,
) -> TextOtelTraceObserver:
    base_config = OtlpHttpTextExporterConfig.from_env(
        os.environ if env is None else env
    )
    setup = create_otlp_http_text_span_exporter(
        replace(base_config, enabled=True, include_content=True)
    )
    if setup.status != "ready" or setup.exporter is None:
        raise RuntimeError(
            setup.reason or "Langfuse Experiment OTLP exporter is unavailable."
        )
    return TextOtelTraceObserver(
        setup.exporter,
        enabled=True,
        continue_on_error=False,
        include_content=True,
    )


def _evaluations(result: GraderResult) -> list[Evaluation]:
    evaluations = [
        Evaluation(
            name=PRIMARY_REWARD_NAME,
            value=result.reward,
            comment=result.reason,
            metadata={
                "dimensions": {
                    name: getattr(result.dimensions, name).passed
                    for name in DIMENSION_NAMES
                }
            },
        )
    ]
    evaluations.extend(
        Evaluation(
            name=f"agent_eval.dimension.{name}",
            value=dimension_result.passed,
            data_type="BOOLEAN",
            comment=dimension_result.reason,
            metadata={
                "assertions": {
                    assertion_name: assertion_result.passed
                    for assertion_name, assertion_result in dimension_result.assertions.items()
                }
            },
        )
        for name in DIMENSION_NAMES
        for dimension_result in [getattr(result.dimensions, name)]
    )
    return evaluations


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
