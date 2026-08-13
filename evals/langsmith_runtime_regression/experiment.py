"""Native LangSmith Dataset replay through the production Assistant runtime."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
import time
from typing import Any, Protocol
from uuid import uuid4

from langsmith.utils import LangSmithRateLimitError

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.runtime_regression_contract import (
    request_text,
    validate_failure_baseline,
)
from assistant_agent.runtime.requests import UserRequest
from evals.langsmith_feedback import normalize_boolean_feedback_score
from evals.langsmith_runtime_regression.evaluators import (
    REQUIRED_LANGSMITH_FEEDBACK_KEYS,
    langsmith_evaluator_output,
)
from evals.release_review.report import LangSmithTargetEvidence


class RuntimeRegressionRuntime(Protocol):
    trace_store: Any

    async def arun_state(self, request: UserRequest) -> Any: ...

    def close(self) -> bool: ...


@dataclass(frozen=True)
class LangSmithRuntimeRegressionSettings:
    model: str
    runtime_factory: Callable[[], RuntimeRegressionRuntime]
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


@dataclass(frozen=True)
class NativeGraphCompletenessResult:
    complete: bool
    run_ids: tuple[str, ...]
    problems: dict[str, tuple[str, ...]]


def runtime_regression_equivalence_evidence(
    result: LangSmithRuntimeRegressionResult,
    completeness: LangSmithCompletenessResult,
) -> LangSmithTargetEvidence:
    """Project only persisted Runtime Regression facts into Gate P3."""

    return LangSmithTargetEvidence(
        target="runtime_regression",
        dataset_id=result.dataset_id,
        project_id=result.experiment_id,
        experiment_id=result.experiment_id,
        active_example_ids=result.example_ids,
        root_run_ids=completeness.run_ids,
        required_feedback=REQUIRED_LANGSMITH_FEEDBACK_KEYS,
        feedback=completeness.feedback,
        native_tree_complete=True,
    )


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


async def run_langsmith_runtime_regression_experiment(
    client: Any,
    settings: LangSmithRuntimeRegressionSettings,
) -> LangSmithRuntimeRegressionResult:
    """Replay active LangSmith examples through the production Runtime."""

    dataset, examples = inspect_langsmith_runtime_regression_dataset(client)
    example_ids = tuple(_require_example_id(example) for example in examples)
    experiment_metadata = {
        "evaluation_mode": "runtime_regression",
        "model": settings.model,
        "git_commit": settings.git_commit,
    }
    project = client.create_project(
        f"{settings.run_name}-{uuid4().hex[:8]}",
        reference_dataset_id=dataset.id,
        metadata=experiment_metadata,
        num_examples=len(examples),
        evaluator_keys=list(REQUIRED_LANGSMITH_FEEDBACK_KEYS),
    )

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        current_run = _current_run_tree()
        example_id = str(_field(current_run, "reference_example_id") or "")
        if (
            current_run is None
            or not _field(current_run, "id")
            or not _field(current_run, "trace_id")
            or example_id not in example_ids
        ):
            raise RuntimeError(
                "LangSmith Experiment target has no matching current RunTree"
            )
        runtime = settings.runtime_factory()
        try:
            state = await runtime.arun_state(
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
            events = runtime.trace_store.list_by_run(state.run_id)
            return langsmith_evaluator_output(state, events)
        finally:
            if runtime.close() is False:
                raise RuntimeError(
                    f"LangSmith Experiment runtime for {example_id!r} failed to close"
                )

    native = await client.aevaluate(
        target,
        data=examples,
        evaluators=[],
        experiment=project,
        blocking=True,
        error_handling="log",
        max_concurrency=settings.max_concurrency,
        metadata=experiment_metadata,
    )
    rows = [row async for row in native]
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
    native_dataset_id = await native.get_dataset_id()
    return LangSmithRuntimeRegressionResult(
        native_result=native,
        experiment_id=str(native.experiment_id),
        experiment_name=str(native.experiment_name),
        experiment_url=(str(native.url) if getattr(native, "url", None) else None),
        dataset_id=str(native_dataset_id or dataset.id),
        example_ids=example_ids,
        run_ids=run_ids,
    )


def wait_for_langsmith_runtime_regression_completeness(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LangSmithCompletenessResult:
    """Wait for one complete Runtime subtree and all UI Feedback per example."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("completeness timeout and poll interval must be positive")
    attempts = math.ceil(timeout_seconds / poll_interval_seconds) + 1
    deadline = clock() + timeout_seconds
    latest_problems: dict[str, list[str]] = {}
    for attempt in range(attempts):
        if attempt > 0 and clock() >= deadline:
            break
        try:
            result, latest_problems = _audit_experiment(
                client,
                experiment_id=experiment_id,
                example_ids=example_ids,
            )
        except LangSmithRateLimitError:
            result = None
            latest_problems = {
                example_id: ["LangSmith completeness query rate limited"]
                for example_id in example_ids
            }
        if result is not None:
            return result
        if attempt + 1 < attempts:
            remaining = max(0.0, deadline - clock())
            if remaining <= 0:
                break
            sleep(min(poll_interval_seconds, remaining))
    raise RuntimeError("LangSmith Experiment incomplete: " + repr(latest_problems))


def audit_native_graph_tree(
    runs: Sequence[Any],
    *,
    example_ids: tuple[str, ...],
) -> NativeGraphCompletenessResult:
    """Audit native LangGraph parentage from persisted LangSmith run facts."""

    runs_by_id = {str(_field(run, "id")): run for run in runs}
    duplicate_run_ids = len(runs_by_id) != len(runs)
    roots_by_example: dict[str, list[Any]] = {
        example_id: [] for example_id in example_ids
    }
    for run in runs:
        example_id = str(_field(run, "reference_example_id") or "")
        if example_id in roots_by_example and _field(run, "parent_run_id") is None:
            roots_by_example[example_id].append(run)

    claimed_run_ids: set[str] = set()
    for roots in roots_by_example.values():
        if len(roots) != 1:
            continue
        root_id = str(_field(roots[0], "id"))
        claimed_run_ids.add(root_id)
        claimed_run_ids.update(
            str(_field(run, "id"))
            for run in runs
            if _is_descendant(run, ancestor_id=root_id, runs_by_id=runs_by_id)
        )
    native_names = {
        "AssistantTurnGraph",
        "assistant",
        "compose_response",
        "execute_tool",
        "llm.chat",
    }
    detached_native_runs = [
        run
        for run in runs
        if _field(run, "name") in native_names
        and str(_field(run, "id")) not in claimed_run_ids
    ]

    problems: dict[str, tuple[str, ...]] = {}
    root_ids: list[str] = []
    for example_id in example_ids:
        item_problems: list[str] = []
        matching_roots = roots_by_example[example_id]
        if duplicate_run_ids:
            item_problems.append("duplicate run id")
        if detached_native_runs:
            item_problems.append("detached native graph run detected")
        if len(matching_roots) != 1:
            item_problems.append(f"root_run_count={len(matching_roots)}")
            problems[example_id] = tuple(item_problems)
            continue

        root = matching_roots[0]
        root_id = str(_field(root, "id"))
        root_ids.append(root_id)
        root_trace_id = str(_field(root, "trace_id") or "")
        if not root_trace_id:
            item_problems.append("root trace missing")
        if _field(root, "run_type") != "chain":
            item_problems.append(
                f"experiment task run_type={_field(root, 'run_type')!r}, "
                "expected 'chain'"
            )
        if not isinstance(_field(root, "inputs"), dict) or not _field(root, "inputs"):
            item_problems.append("root inputs missing or not an object")
        if not isinstance(_field(root, "outputs"), dict) or not _field(root, "outputs"):
            item_problems.append("root outputs missing or not an object")

        subtree = [
            run
            for run in runs
            if _is_descendant(run, ancestor_id=root_id, runs_by_id=runs_by_id)
        ]
        if any(str(_field(run, "trace_id") or "") != root_trace_id for run in subtree):
            item_problems.append("trace mismatch")
        if any(
            str(_field(run, "reference_example_id") or "") not in ("", example_id)
            for run in subtree
        ):
            item_problems.append("reference example mismatch")

        graphs = [
            run
            for run in runs
            if _field(run, "name") == "AssistantTurnGraph"
            and str(_field(run, "parent_run_id") or "") == root_id
            and str(_field(run, "trace_id") or "") == root_trace_id
        ]
        if len(graphs) != 1:
            item_problems.append(f"AssistantTurnGraph child count={len(graphs)}")
        else:
            if _field(graphs[0], "run_type") != "chain":
                item_problems.append(
                    "AssistantTurnGraph "
                    f"run_type={_field(graphs[0], 'run_type')!r}, expected 'chain'"
                )
            graph_id = str(_field(graphs[0], "id"))
            graph_subtree = [
                run
                for run in runs
                if str(_field(run, "trace_id") or "") == root_trace_id
                and _is_descendant(
                    run,
                    ancestor_id=graph_id,
                    runs_by_id=runs_by_id,
                )
            ]
            assistants = [
                run
                for run in graph_subtree
                if _field(run, "name") == "assistant"
                and str(_field(run, "parent_run_id") or "") == graph_id
            ]
            if not assistants:
                item_problems.append("missing assistant graph child")
            for assistant in assistants:
                if _field(assistant, "run_type") != "chain":
                    item_problems.append(
                        "assistant "
                        f"run_type={_field(assistant, 'run_type')!r}, "
                        "expected 'chain'"
                    )
            compose_responses = [
                run
                for run in graph_subtree
                if _field(run, "name") == "compose_response"
                and str(_field(run, "parent_run_id") or "") == graph_id
            ]
            if not compose_responses:
                item_problems.append("missing compose_response graph child")
            for compose_response in compose_responses:
                if _field(compose_response, "run_type") != "chain":
                    item_problems.append(
                        "compose_response "
                        f"run_type={_field(compose_response, 'run_type')!r}, "
                        "expected 'chain'"
                    )
            nested_llm_runs = [
                run
                for run in graph_subtree
                if _field(run, "name") == "llm.chat"
                and any(
                    _is_descendant(
                        run,
                        ancestor_id=str(_field(assistant, "id")),
                        runs_by_id=runs_by_id,
                    )
                    for assistant in assistants
                )
            ]
            valid_llm_runs = [
                run for run in nested_llm_runs if _field(run, "run_type") == "llm"
            ]
            for llm_run in nested_llm_runs:
                if _field(llm_run, "run_type") != "llm":
                    item_problems.append(
                        "llm.chat "
                        f"run_type={_field(llm_run, 'run_type')!r}, "
                        "expected 'llm'"
                    )
            if not valid_llm_runs:
                item_problems.append("missing llm.chat in graph subtree")
            nested_llm_ids = {str(_field(run, "id")) for run in nested_llm_runs}
            if any(
                _field(run, "name") == "llm.chat"
                and str(_field(run, "id")) not in nested_llm_ids
                for run in subtree
            ):
                item_problems.append("llm.chat outside assistant subtree")

            execute_tool_ids = {
                str(_field(run, "id"))
                for run in graph_subtree
                if _field(run, "name") == "execute_tool"
            }
            for execute_tool in graph_subtree:
                if (
                    _field(execute_tool, "name") == "execute_tool"
                    and _field(execute_tool, "run_type") != "chain"
                ):
                    item_problems.append(
                        "execute_tool "
                        f"run_type={_field(execute_tool, 'run_type')!r}, "
                        "expected 'chain'"
                    )
            tool_runs = [run for run in subtree if _field(run, "run_type") == "tool"]
            for tool_run in tool_runs:
                if not any(
                    _is_descendant(
                        tool_run,
                        ancestor_id=execute_tool_id,
                        runs_by_id=runs_by_id,
                    )
                    for execute_tool_id in execute_tool_ids
                ):
                    item_problems.append("governed tool outside execute_tool subtree")
                    break
        if item_problems:
            problems[example_id] = tuple(item_problems)

    return NativeGraphCompletenessResult(
        complete=not problems,
        run_ids=tuple(root_ids),
        problems=problems,
    )


def _audit_experiment(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
) -> tuple[LangSmithCompletenessResult | None, dict[str, list[str]]]:
    runs = list(
        client.list_runs(
            project_id=experiment_id,
            select=[
                "id",
                "parent_run_id",
                "name",
                "run_type",
                "reference_example_id",
                "trace_id",
                "inputs",
                "outputs",
            ],
        )
    )
    tree_audit = audit_native_graph_tree(runs, example_ids=example_ids)
    problems = {
        example_id: list(item_problems)
        for example_id, item_problems in tree_audit.problems.items()
    }
    run_ids = tree_audit.run_ids
    feedback_by_example: dict[str, dict[str, Any]] = {
        example_id: {} for example_id in example_ids
    }
    run_to_example = {
        str(_field(run, "id")): str(_field(run, "reference_example_id"))
        for run in runs
        if _field(run, "parent_run_id") is None
        and str(_field(run, "reference_example_id") or "") in example_ids
    }
    if run_ids:
        for feedback in client.list_feedback(run_ids=run_ids):
            example_id = run_to_example.get(str(_field(feedback, "run_id")))
            key = _field(feedback, "key")
            if example_id is None or key not in REQUIRED_LANGSMITH_FEEDBACK_KEYS:
                continue
            if str(key) in feedback_by_example[example_id]:
                problems.setdefault(example_id, []).append(f"duplicate feedback {key}")
                continue
            try:
                score = normalize_boolean_feedback_score(_field(feedback, "score"))
            except ValueError:
                problems.setdefault(example_id, []).append(f"invalid feedback {key}")
                continue
            feedback_by_example[example_id][str(key)] = score
    required_feedback = set(REQUIRED_LANGSMITH_FEEDBACK_KEYS)
    for example_id in example_ids:
        missing = sorted(
            key
            for key in required_feedback
            if feedback_by_example[example_id].get(key) is None
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


def _is_descendant(
    run: Any,
    *,
    ancestor_id: str,
    runs_by_id: dict[str, Any],
) -> bool:
    parent_id = str(_field(run, "parent_run_id") or "")
    visited: set[str] = set()
    while parent_id and parent_id not in visited:
        if parent_id == ancestor_id:
            return True
        visited.add(parent_id)
        parent = runs_by_id.get(parent_id)
        if parent is None:
            return False
        parent_id = str(_field(parent, "parent_run_id") or "")
    return False


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


def _current_run_tree() -> Any | None:
    from langsmith.run_helpers import get_current_run_tree

    return get_current_run_tree()
