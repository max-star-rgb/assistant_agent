"""Thin Langfuse Dataset and Experiment backend for Git-owned tasks."""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Mapping
from copy import copy
from dataclasses import replace
import time
from typing import Any

from langfuse import Evaluation, Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    create_otlp_http_text_span_exporter,
)
from evals.agent.calibration import load_calibration_set
from evals.agent.contracts import (
    DimensionResult,
    GraderResult,
    LLMJudge,
    RunEvidence,
    TaskEnvironment,
    TaskSpec,
)
from evals.agent.grading import (
    DIMENSION_NAMES,
    grade_task,
    validate_mission_objective_assertions,
)
from evals.agent.judge import ProgressCallback
from evals.agent.loader import load_case_source, load_entrypoint, load_task


DEFAULT_DATASET_NAME = "assistant-agent-regression"
QUALITY_SCORE_PREFIX = "assistant_agent.quality."
EXPERIMENT_SCORE_DIMENSIONS = (
    ("task_conformance", "tool_execution"),
    ("grounding", "grounding"),
    ("response_quality", "response_quality"),
)


def active_dataset_task_ids(
    client: Langfuse,
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
) -> list[str]:
    dataset = client.get_dataset(dataset_name)
    task_ids: list[str] = []
    for item in dataset.items:
        if _item_status(item) != "ACTIVE":
            continue
        item_input = _item_field(item, "input")
        metadata = _item_field(item, "metadata")
        if not isinstance(item_input, dict) or not isinstance(metadata, dict):
            raise RuntimeError(
                "Active Dataset items must use the Agent eval item contract."
            )
        task_id = item_input.get("task_id")
        if (
            not isinstance(task_id, str)
            or not task_id
            or metadata.get("task_id") != task_id
        ):
            raise RuntimeError(
                "Active Dataset item task_id is missing or inconsistent."
            )
        if task_id in task_ids:
            raise RuntimeError(
                f"Active Dataset contains duplicate task_id: {task_id}."
            )
        task_ids.append(task_id)
    return task_ids


def publish_tasks(
    client: Langfuse,
    tasks: Collection[TaskSpec],
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
) -> list[str]:
    for task in tasks:
        _validate_task_definition_for_publish(task)
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


def _validate_task_definition_for_publish(task: TaskSpec) -> None:
    source = load_case_source(task.id)
    environment_type = load_entrypoint(task.environment)
    environment: TaskEnvironment = environment_type()
    environment.validate().require_valid()
    grader = load_entrypoint(task.grader)
    if not callable(grader):
        raise RuntimeError(f"Agent eval grader is not callable: {task.grader}.")
    load_calibration_set(task.id)
    if source.level != "mission":
        return
    objective_method = getattr(
        environment,
        "objective_state_assertions",
        None,
    )
    if not callable(objective_method):
        raise RuntimeError(
            f"Mission {task.id!r} must define objective_state_assertions()."
        )
    validate_mission_objective_assertions(
        objective_method(
            RunEvidence(
                task_id=task.id,
                run_id="publish-validation",
                trace_id="0" * 32,
                terminal_status="not_run",
            )
        )
    )


def run_tasks(
    client: Langfuse,
    tasks: Collection[TaskSpec],
    *,
    config: ProviderConfig,
    judge: LLMJudge,
    dataset_name: str = DEFAULT_DATASET_NAME,
    run_name: str | None = None,
    trace_observer: TextOtelTraceObserver | None = None,
    progress: ProgressCallback | None = None,
    active_only: bool = False,
) -> Any:
    task_by_id = {task.id: task for task in tasks}
    dataset = client.get_dataset(dataset_name)
    selected_items = [
        item
        for item in dataset.items
        if isinstance(item.metadata, dict)
        and item.metadata.get("task_id") in task_by_id
        and _item_status(item) == "ACTIVE"
    ]
    selected_task_ids = [
        str(item.metadata["task_id"])
        for item in selected_items
        if isinstance(item.metadata, dict)
    ]
    duplicates = sorted(
        task_id
        for task_id in set(selected_task_ids)
        if selected_task_ids.count(task_id) > 1
    )
    if duplicates:
        raise RuntimeError(
            "Published Dataset contains duplicate ACTIVE Dataset items for: "
            + ", ".join(duplicates)
        )
    missing = set(task_by_id) - set(selected_task_ids)
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
        started_at = time.perf_counter()
        _report_progress(
            progress,
            "agent_eval.task.started",
            task_id=str(task_id),
        )
        trace_id = client.get_current_trace_id()
        parent_span_id = client.get_current_observation_id()
        if not trace_id or not parent_span_id:
            raise RuntimeError("Langfuse Experiment trace context is unavailable.")
        environment_type = load_entrypoint(task.environment)
        environment: TaskEnvironment = environment_type(config=config)
        try:
            execution = environment.execute(
                task=task,
                request=item_input.get("request"),
                trace_id=trace_id,
                parent_span_id=parent_span_id,
            )
        except Exception as exc:
            _report_progress(
                progress,
                "agent_eval.task.failed",
                task_id=str(task_id),
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                error_type=type(exc).__name__,
            )
            raise
        if trace_observer is not None:
            for event in execution.trace_events:
                trace_observer.on_trace_event(event)
        _report_progress(
            progress,
            "agent_eval.task.completed",
            task_id=str(task_id),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
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
        started_at = time.perf_counter()
        _report_progress(
            progress,
            "agent_eval.evaluation.started",
            task_id=str(task_id),
        )
        try:
            result: GraderResult = grade_task(
                task=task,
                evidence=RunEvidence.model_validate(output),
                judge=judge,
            )
        except Exception as exc:
            _report_progress(
                progress,
                "agent_eval.evaluation.failed",
                task_id=str(task_id),
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                error_type=type(exc).__name__,
            )
            raise
        _report_progress(
            progress,
            "agent_eval.evaluation.completed",
            task_id=str(task_id),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            dimensions={
                name: getattr(result.dimensions, name).passed
                for name in DIMENSION_NAMES
            },
        )
        return _evaluations(result)

    return _run_experiment_preserving_evaluator_errors(
        selected_dataset.run_experiment,
        evaluator=evaluate_item,
        name="assistant-agent-regression",
        run_name=run_name,
        description=(
            "Run Git-owned task environments through AgentGraphRuntime and "
            "score them with task-local graders."
        ),
        task=execute_item,
        max_concurrency=1,
        metadata={
            "framework": "evals/agent",
            "task_count": len(task_by_id),
        },
    )


def _run_experiment_preserving_evaluator_errors(
    run_experiment: Callable[..., Any],
    *,
    evaluator: Callable[..., list[Evaluation]],
    **kwargs: Any,
) -> Any:
    """Restore evaluator failures that the Langfuse SDK records but swallows."""

    failures: list[Exception] = []

    def monitored_evaluator(**evaluator_kwargs: Any) -> list[Evaluation]:
        try:
            return evaluator(**evaluator_kwargs)
        except Exception as exc:
            failures.append(exc)
            raise

    result = run_experiment(
        evaluators=[monitored_evaluator],
        **kwargs,
    )
    if failures:
        failure = failures[0]
        raise RuntimeError(
            f"Agent eval evaluator failed: {failure}"
        ) from failure
    return result


def experiment_dimension_scores(result: Any) -> list[dict[str, bool]]:
    expected_names = {
        f"{QUALITY_SCORE_PREFIX}{score_name}": score_name
        for score_name, _ in EXPERIMENT_SCORE_DIMENSIONS
    }
    item_scores: list[dict[str, bool]] = []
    for item_result in result.item_results:
        resolved: dict[str, bool] = {}
        for evaluation in item_result.evaluations:
            dimension_name = expected_names.get(evaluation.name)
            if dimension_name is None:
                continue
            if not isinstance(evaluation.value, bool):
                raise RuntimeError(
                    f"Experiment dimension {evaluation.name} is not BOOLEAN."
                )
            if dimension_name in resolved:
                raise RuntimeError(
                    "Experiment result contains duplicate Agent eval dimension: "
                    f"{evaluation.name}."
                )
            resolved[dimension_name] = evaluation.value
        expected_dimensions = {name for name, _ in EXPERIMENT_SCORE_DIMENSIONS}
        if set(resolved) != expected_dimensions:
            raise RuntimeError(
                "Experiment result is missing Agent eval dimensions: "
                f"expected={sorted(expected_dimensions)}, actual={sorted(resolved)}."
            )
        item_scores.append(resolved)
    if not item_scores:
        raise RuntimeError("Experiment result contains no evaluated items.")
    return item_scores


def verify_persisted_dimension_scores(
    client: Langfuse,
    result: Any,
    *,
    attempts: int = 30,
    retry_delay_seconds: float = 0.5,
) -> None:
    """Verify every Experiment item persisted the canonical task-level Scores."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1.")
    expected_names = {
        f"{QUALITY_SCORE_PREFIX}{score_name}"
        for score_name, _ in EXPERIMENT_SCORE_DIMENSIONS
    }
    client.flush()
    for item_result in result.item_results:
        trace_id = getattr(item_result, "trace_id", None)
        if not isinstance(trace_id, str) or not trace_id:
            raise RuntimeError(
                "Experiment item is missing trace_id for persisted Score verification."
            )
        failure_detail = ""
        for attempt in range(attempts):
            observations_response = client.api.legacy.observations_v1.get_many(
                trace_id=trace_id,
                name="experiment-item-task",
                type="SPAN",
                limit=2,
            )
            observations = observations_response.data
            if len(observations) != 1:
                failure_detail = (
                    f"trace_id={trace_id}, expected exactly one "
                    "experiment-item-task observation, "
                    f"actual={len(observations)}"
                )
                if attempt + 1 < attempts and retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
                continue
            task_observation_id = getattr(observations[0], "id", None)
            if not isinstance(task_observation_id, str) or not task_observation_id:
                failure_detail = (
                    f"trace_id={trace_id}, experiment-item-task observation "
                    "is missing id"
                )
                if attempt + 1 < attempts and retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
                continue

            response = client.api.scores_v3.get_many_v3(
                limit=100,
                fields="subject",
                name=",".join(sorted(expected_names)),
                trace_id=trace_id,
            )
            scores = [
                score
                for score in response.data
                if score.name in expected_names
            ]
            names = [score.name for score in scores]
            missing = sorted(expected_names - set(names))
            duplicates = sorted(
                name for name in expected_names if names.count(name) > 1
            )
            if missing or duplicates:
                failure_detail = (
                    f"trace_id={trace_id}, missing={missing}, "
                    f"duplicates={duplicates}"
                )
            else:
                invalid_records: list[str] = []
                for score in scores:
                    subject = getattr(score, "subject", None)
                    if getattr(score, "data_type", None) != "BOOLEAN":
                        invalid_records.append(
                            f"{score.name}: data_type="
                            f"{getattr(score, 'data_type', None)!r}"
                        )
                    elif subject is None:
                        invalid_records.append(f"{score.name}: missing subject")
                    elif getattr(subject, "kind", None) != "observation":
                        invalid_records.append(
                            f"{score.name}: subject.kind="
                            f"{getattr(subject, 'kind', None)!r}"
                        )
                    elif getattr(subject, "trace_id", None) != trace_id:
                        invalid_records.append(
                            f"{score.name}: subject.trace_id="
                            f"{getattr(subject, 'trace_id', None)!r}"
                        )
                    elif getattr(subject, "id", None) != task_observation_id:
                        invalid_records.append(
                            f"{score.name}: subject.id does not match "
                            "experiment-item-task observation"
                        )
                if not invalid_records:
                    break
                failure_detail = (
                    f"trace_id={trace_id}, invalid={invalid_records}"
                )
            if attempt + 1 < attempts and retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
        else:
            raise RuntimeError(
                "Experiment is missing persisted Agent eval dimensions on one "
                f"experiment-item-task observation: {failure_detail}."
            )


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
    return [
        Evaluation(
            name=f"{QUALITY_SCORE_PREFIX}{score_name}",
            value=dimension_result.passed,
            data_type="BOOLEAN",
            comment=dimension_result.reason,
            metadata=_assertion_metadata(dimension_result),
        )
        for score_name, dimension_name in EXPERIMENT_SCORE_DIMENSIONS
        for dimension_result in [getattr(result.dimensions, dimension_name)]
    ]


def _assertion_metadata(
    dimension_result: DimensionResult,
) -> dict[str, bool | str]:
    metadata: dict[str, bool | str] = {}
    for assertion_name, assertion_result in dimension_result.assertions.items():
        prefix = f"assertion.{assertion_name}"
        metadata[f"{prefix}.passed"] = assertion_result.passed
        metadata[f"{prefix}.label"] = assertion_result.label
        metadata[f"{prefix}.method"] = assertion_result.evaluation_method
        if assertion_result.criterion_id is not None:
            metadata[f"{prefix}.criterion_id"] = assertion_result.criterion_id
    return metadata


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _item_status(item: Any) -> Any:
    status = _item_field(item, "status")
    return getattr(status, "value", status)


def _report_progress(
    callback: ProgressCallback | None,
    event: str,
    **details: object,
) -> None:
    if callback is not None:
        callback({"event": event, **details})
