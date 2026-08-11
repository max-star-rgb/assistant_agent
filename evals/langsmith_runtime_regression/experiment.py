"""Native LangSmith Dataset replay through the production Assistant runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import time
from typing import Any, Protocol

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.langsmith_trace import (
    LangSmithExperimentBinding,
    current_langsmith_experiment_binding,
)
from assistant_agent.evaluation.runtime_regression_contract import (
    assistant_output,
    request_text,
    validate_failure_baseline,
)
from assistant_agent.runtime.requests import UserRequest


REQUIRED_LANGSMITH_FEEDBACK_KEYS = (
    "assistant_agent.quality.response_quality.experiment",
    "assistant_agent.quality.grounding.experiment",
    "assistant_agent.quality.regression_improvement.experiment",
)


class RuntimeRegressionRuntime(Protocol):
    trace_store: Any

    def run_state(self, request: UserRequest) -> Any: ...

    def close(self) -> bool: ...


@dataclass(frozen=True)
class LangSmithRuntimeRegressionSettings:
    model: str
    runtime_factory: Callable[
        [LangSmithExperimentBinding], RuntimeRegressionRuntime
    ]
    run_name: str
    git_commit: str
    max_concurrency: int = 1


@dataclass(frozen=True)
class LangSmithRuntimeRegressionResult:
    native_result: Any
    experiment_id: str
    experiment_name: str
    experiment_url: str | None
    dataset_id: str
    example_ids: tuple[str, ...]
    run_ids: tuple[str, ...]


@dataclass(frozen=True)
class LangSmithCompletenessResult:
    run_ids: tuple[str, ...]
    feedback: dict[str, dict[str, Any]]


def inspect_langsmith_runtime_regression_dataset(
    client: Any,
) -> tuple[Any, list[Any]]:
    """Load and validate active LangSmith examples without running a model."""

    dataset = client.read_dataset(dataset_name=RUNTIME_REGRESSION_DATASET)
    examples = sorted(
        (
            example
            for example in client.list_examples(dataset_id=dataset.id)
            if _example_active(example)
        ),
        key=lambda example: str(_field(example, "id")),
    )
    if not examples:
        raise RuntimeError("runtime regression Dataset has no active examples")
    for example in examples:
        example_id = _require_example_id(example)
        inputs = _field(example, "inputs")
        if not isinstance(inputs, dict):
            raise RuntimeError(
                f"runtime regression item {example_id!r} input must be an object"
            )
        request_text(example_id, inputs)
        validate_failure_baseline(example_id, _field(example, "outputs"))
    return dataset, examples


def run_langsmith_runtime_regression_experiment(
    client: Any,
    settings: LangSmithRuntimeRegressionSettings,
) -> LangSmithRuntimeRegressionResult:
    """Replay active LangSmith examples through the production Runtime."""

    dataset, examples = inspect_langsmith_runtime_regression_dataset(client)
    example_ids = tuple(_require_example_id(example) for example in examples)

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        binding = current_langsmith_experiment_binding()
        if binding is None or binding.trace_context.experiment_link is None:
            raise RuntimeError(
                "LangSmith Experiment target has no active RunTree binding"
            )
        link = binding.trace_context.experiment_link
        example_id = link.reference_example_id
        runtime = settings.runtime_factory(binding)
        try:
            state = runtime.run_state(
                UserRequest(
                    user_id="runtime-regression",
                    session_id=f"runtime-regression-{example_id}",
                    text=request_text(example_id, inputs),
                    metadata={
                        "runtime_regression": {
                            "dataset_item_id": example_id,
                            "backend": "langsmith",
                        }
                    },
                )
            )
            return assistant_output(state)
        finally:
            runtime.close()

    native = client.evaluate(
        target,
        data=examples,
        evaluators=[],
        experiment_prefix=settings.run_name,
        blocking=True,
        error_handling="log",
        max_concurrency=settings.max_concurrency,
        metadata={
            "evaluation_mode": "runtime_regression",
            "model": settings.model,
            "git_commit": settings.git_commit,
        },
    )
    rows = list(native)
    rows_by_example: dict[str, Any] = {}
    for row in rows:
        row_example = _field(row, "example")
        row_example_id = _require_example_id(row_example)
        if row_example_id in rows_by_example:
            raise RuntimeError(
                f"LangSmith Experiment produced duplicate row for {row_example_id!r}"
            )
        rows_by_example[row_example_id] = row
    missing_rows = sorted(set(example_ids) - set(rows_by_example))
    if missing_rows:
        raise RuntimeError(
            f"LangSmith Experiment produced no row for examples {missing_rows!r}"
        )
    run_ids = tuple(
        _require_run_id(_field(rows_by_example[example_id], "run"))
        for example_id in example_ids
    )
    return LangSmithRuntimeRegressionResult(
        native_result=native,
        experiment_id=str(native.experiment_id),
        experiment_name=str(native.experiment_name),
        experiment_url=(str(native.url) if getattr(native, "url", None) else None),
        dataset_id=str(native.get_dataset_id() or dataset.id),
        example_ids=example_ids,
        run_ids=run_ids,
    )


def wait_for_langsmith_runtime_regression_completeness(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LangSmithCompletenessResult:
    """Wait for one complete Runtime subtree and all UI Feedback per example."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("completeness timeout and poll interval must be positive")
    attempts = math.floor(timeout_seconds / poll_interval_seconds) + 1
    latest_problems: dict[str, list[str]] = {}
    for attempt in range(attempts):
        result, latest_problems = _audit_experiment(
            client,
            experiment_id=experiment_id,
            example_ids=example_ids,
        )
        if result is not None:
            return result
        if attempt + 1 < attempts:
            sleep(poll_interval_seconds)
    raise RuntimeError(
        "LangSmith Experiment incomplete: " + repr(latest_problems)
    )


def _audit_experiment(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
) -> tuple[LangSmithCompletenessResult | None, dict[str, list[str]]]:
    expected = set(example_ids)
    roots_by_example: dict[str, list[Any]] = {key: [] for key in expected}
    for root in client.list_runs(project_id=experiment_id, is_root=True):
        example_id = str(_field(root, "reference_example_id") or "")
        if example_id in roots_by_example:
            roots_by_example[example_id].append(root)

    problems: dict[str, list[str]] = {}
    roots: dict[str, Any] = {}
    for example_id in example_ids:
        matching = roots_by_example[example_id]
        item_problems: list[str] = []
        if len(matching) != 1:
            item_problems.append(f"root_run_count={len(matching)}")
        else:
            root = matching[0]
            roots[example_id] = root
            if not isinstance(_field(root, "inputs"), dict) or not _field(
                root, "inputs"
            ):
                item_problems.append("root inputs missing or not an object")
            if not isinstance(_field(root, "outputs"), dict) or not _field(
                root, "outputs"
            ):
                item_problems.append("root outputs missing or not an object")
            trace_names = {
                str(_field(run, "name"))
                for run in client.list_runs(trace_id=_field(root, "trace_id"))
            }
            for required_name in ("agent.runtime", "llm.chat"):
                if required_name not in trace_names:
                    item_problems.append(f"missing trace span {required_name}")
        if item_problems:
            problems[example_id] = item_problems

    run_ids = tuple(
        str(_field(roots[example_id], "id"))
        for example_id in example_ids
        if example_id in roots
    )
    feedback_by_example: dict[str, dict[str, Any]] = {
        example_id: {} for example_id in example_ids
    }
    run_to_example = {
        str(_field(root, "id")): example_id
        for example_id, root in roots.items()
    }
    if run_ids:
        for feedback in client.list_feedback(run_ids=run_ids):
            example_id = run_to_example.get(str(_field(feedback, "run_id")))
            key = _field(feedback, "key")
            if example_id is None or key not in REQUIRED_LANGSMITH_FEEDBACK_KEYS:
                continue
            feedback_by_example[example_id][str(key)] = _field(feedback, "score")
    required_feedback = set(REQUIRED_LANGSMITH_FEEDBACK_KEYS)
    for example_id in example_ids:
        missing = sorted(
            required_feedback - set(feedback_by_example[example_id])
        )
        if missing:
            problems.setdefault(example_id, []).append(
                "missing feedback " + repr(missing)
            )
    if problems:
        return None, problems
    return (
        LangSmithCompletenessResult(
            run_ids=run_ids,
            feedback=feedback_by_example,
        ),
        {},
    )


def _example_active(example: Any) -> bool:
    metadata = _field(example, "metadata")
    return not isinstance(metadata, dict) or metadata.get("active", True) is not False


def _require_example_id(example: Any) -> str:
    value = _field(example, "id")
    if value is None or not str(value):
        raise RuntimeError("runtime regression Dataset example has no id")
    return str(value)


def _require_run_id(run: Any) -> str:
    value = _field(run, "id")
    if value is None or not str(value):
        raise RuntimeError("LangSmith Experiment result row has no run id")
    return str(value)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)
