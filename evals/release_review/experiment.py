"""Native LangSmith Release Review over the production Assistant graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.tool_execution_backend import ToolExecutionBackend

from .assertions import evaluate_task_conformance
from .contracts import ReleaseScenario
from .decision_backend import ScenarioExecutionBackend
from .evidence import ReleaseRunEvidence
from .langsmith_backend import (
    GIT_EXAMPLE_OWNER,
    RELEASE_REVIEW_DATASET,
    REQUIRED_RELEASE_FEEDBACK_KEYS,
)
from .loader import scenario_hash
from .staging import CleanupResult, StagingResourceManager


TASK_CONFORMANCE_FEEDBACK_KEY = "assistant_agent.quality.task_conformance"


class ReleaseRuntime(Protocol):
    trace_store: Any

    async def arun_state(self, request: UserRequest) -> Any: ...

    def close(self) -> bool: ...


RuntimeFactory = Callable[
    [ReleaseScenario, ToolExecutionBackend | None, Mapping[str, Any]], ReleaseRuntime
]
ProgressCallback = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class ReleaseExperimentSettings:
    release_id: str
    model: str
    git_commit: str
    catalog_generation: str
    evaluator_version: str
    runtime_factory: RuntimeFactory
    staging_resources: StagingResourceManager | None = None
    run_name: str | None = None
    max_concurrency: int = 4
    staging_concurrency: int = 2
    deadline_monotonic: float | None = None


@dataclass(frozen=True)
class ReleaseExperimentResult:
    native_result: Any
    experiment_id: str
    experiment_name: str
    experiment_url: str | None
    dataset_id: str
    example_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    cleanup_results: dict[str, CleanupResult] = field(default_factory=dict)
    expected_item_keys: tuple[str, ...] = ()

    @property
    def run_name(self) -> str:
        return self.experiment_name

    @property
    def dataset_run_id(self) -> str:
        return self.experiment_id

    @property
    def dataset_run_url(self) -> str | None:
        return self.experiment_url


async def run_release_experiment(
    client: Any,
    scenarios: Collection[ReleaseScenario],
    settings: ReleaseExperimentSettings,
    progress: ProgressCallback | None = None,
) -> ReleaseExperimentResult:
    """Evaluate each selected Git scenario through the actual compiled graph."""

    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    if len(scenario_by_id) != len(scenarios):
        raise ValueError("release scenarios must have unique ids")
    if not scenario_by_id:
        raise RuntimeError("Release Review requires at least one scenario")
    if settings.max_concurrency < 1 or settings.staging_concurrency < 1:
        raise ValueError("Release Review concurrency must be positive")

    dataset, examples, bindings = inspect_release_examples(client, scenarios)
    example_ids = tuple(
        _required_id(example, "Release Review Example") for example in examples
    )
    expected_keys = tuple(
        f"{scenario.id}:r{repetition}"
        for scenario in sorted(scenarios, key=lambda item: item.id)
        for repetition in range(1, scenario.repetitions + 1)
    )
    metadata = {
        "evaluation_mode": "release_review",
        "release_id": settings.release_id,
        "model": settings.model,
        "git_commit": settings.git_commit,
        "catalog_generation": settings.catalog_generation,
        "evaluator_version": settings.evaluator_version,
    }
    project_name = f"{settings.run_name or settings.release_id}-{uuid4().hex[:8]}"
    project = client.create_project(
        project_name,
        reference_dataset_id=_field(dataset, "id"),
        metadata=metadata,
        num_examples=len(examples),
        evaluator_keys=list(REQUIRED_RELEASE_FEEDBACK_KEYS),
    )
    staging_slots = asyncio.Semaphore(settings.staging_concurrency)
    cleanup_results: dict[str, CleanupResult] = {}

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        current = _current_run_tree()
        example_id = str(_field(current, "reference_example_id") or "")
        if (
            current is None
            or not _field(current, "id")
            or not _field(current, "trace_id")
            or example_id not in bindings
        ):
            raise RuntimeError(
                "LangSmith Release Review target has no matching current RunTree"
            )
        scenario, repetition = bindings[example_id]
        if inputs != {"scenario_id": scenario.id, "request": scenario.request}:
            raise RuntimeError(f"Dataset request mismatch for {scenario.id}")
        return await _run_release_item(
            scenario,
            repetition=repetition,
            settings=settings,
            progress=progress,
            staging_slots=staging_slots,
            cleanup_results=cleanup_results,
        )

    def task_conformance_evaluator(run: Any, example: Any) -> dict[str, Any]:
        example_id = _required_id(example, "Release Review Example")
        binding = bindings.get(example_id)
        if binding is None:
            raise RuntimeError("Release Review evaluator received an unknown Example")
        scenario, _repetition = binding
        output = _field(run, "outputs")
        if not isinstance(output, dict) or not isinstance(output.get("evidence"), dict):
            raise RuntimeError("Release Review Experiment output is missing evidence")
        result = evaluate_task_conformance(
            scenario,
            ReleaseRunEvidence.model_validate(output["evidence"]),
        )
        if result.passed is None:
            raise RuntimeError("infrastructure failure cannot produce quality Feedback")
        failed = [item for item in result.assertions if not item.passed]
        visible = failed or list(result.assertions)
        comment = "; ".join(
            f"{item.label}: {item.reason}" if failed else item.label for item in visible
        )
        return {
            "key": TASK_CONFORMANCE_FEEDBACK_KEY,
            "score": bool(result.passed),
            "comment": comment,
            "metadata": {
                "evaluation_mode": "release_review",
                "phase": scenario.phase,
                "release_id": settings.release_id,
                "evaluator_version": settings.evaluator_version,
            },
        }

    native = await client.aevaluate(
        target,
        data=examples,
        evaluators=[task_conformance_evaluator],
        experiment=project,
        blocking=True,
        error_handling="log",
        max_concurrency=settings.max_concurrency,
        metadata=metadata,
    )
    rows = [row async for row in native]
    rows_by_example: dict[str, Any] = {}
    for row in rows:
        example_id = _required_id(_field(row, "example"), "Release Review Example")
        if example_id in rows_by_example:
            raise RuntimeError("LangSmith Release Review returned duplicate row")
        rows_by_example[example_id] = row
    missing = sorted(set(example_ids) - set(rows_by_example))
    if missing:
        raise RuntimeError(f"LangSmith Release Review missing rows {missing!r}")
    run_ids = tuple(
        _required_id(_field(rows_by_example[example_id], "run"), "Release Review run")
        for example_id in example_ids
    )
    native_dataset_id = await native.get_dataset_id()
    return ReleaseExperimentResult(
        native_result=native,
        experiment_id=str(_field(native, "experiment_id") or _field(project, "id")),
        experiment_name=str(
            _field(native, "experiment_name") or _field(project, "name") or project_name
        ),
        experiment_url=(str(_field(native, "url")) if _field(native, "url") else None),
        dataset_id=str(native_dataset_id or _field(dataset, "id")),
        example_ids=example_ids,
        run_ids=run_ids,
        cleanup_results=dict(cleanup_results),
        expected_item_keys=expected_keys,
    )


def inspect_release_examples(
    client: Any,
    scenarios: Collection[ReleaseScenario],
) -> tuple[Any, list[Any], dict[str, tuple[ReleaseScenario, int]]]:
    """Validate active Git-owned Examples for exactly the selected scenarios."""

    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    dataset = client.read_dataset(dataset_name=RELEASE_REVIEW_DATASET)
    dataset_id = _required_id(dataset, "Release Review Dataset")
    selected: dict[tuple[str, int], Any] = {}
    for example in client.list_examples(dataset_id=dataset_id):
        metadata = _metadata(example)
        if (
            metadata.get("owner") != GIT_EXAMPLE_OWNER
            or metadata.get("active", True) is False
        ):
            continue
        scenario_id = metadata.get("scenario_id")
        if scenario_id not in scenario_by_id:
            continue
        repetition = metadata.get("repetition")
        if not isinstance(repetition, int) or isinstance(repetition, bool):
            raise RuntimeError(
                f"Release Review repetition is invalid for {scenario_id}"
            )
        key = str(scenario_id), repetition
        if key in selected:
            raise RuntimeError(f"duplicate Release Review Example {key!r}")
        selected[key] = example

    expected = {
        (scenario.id, repetition)
        for scenario in scenarios
        for repetition in range(1, scenario.repetitions + 1)
    }
    if set(selected) != expected:
        raise RuntimeError(
            "Release Review Examples do not match Git scenarios: "
            f"expected={sorted(expected)!r}, actual={sorted(selected)!r}"
        )
    examples: list[Any] = []
    bindings: dict[str, tuple[ReleaseScenario, int]] = {}
    for key in sorted(expected):
        scenario_id, repetition = key
        scenario = scenario_by_id[scenario_id]
        example = selected[key]
        metadata = _metadata(example)
        if metadata.get("scenario_hash") != scenario_hash(scenario):
            raise RuntimeError(f"scenario hash mismatch for {scenario_id}")
        if _field(example, "inputs") != {
            "scenario_id": scenario_id,
            "request": scenario.request,
        }:
            raise RuntimeError(f"Dataset request mismatch for {scenario_id}")
        example_id = _required_id(example, "Release Review Example")
        examples.append(example)
        bindings[example_id] = scenario, repetition
    return dataset, examples, bindings


async def _run_release_item(
    scenario: ReleaseScenario,
    *,
    repetition: int,
    settings: ReleaseExperimentSettings,
    progress: ProgressCallback | None,
    staging_slots: asyncio.Semaphore,
    cleanup_results: dict[str, CleanupResult],
) -> dict[str, Any]:
    _check_deadline(settings)
    key = f"{scenario.id}:r{repetition}"
    _progress(progress, "release_review.item.started", item_key=key)
    backend: ToolExecutionBackend | None = None
    runtime_metadata: dict[str, Any] = {
        "release_review": {
            "release_id": settings.release_id,
            "scenario_id": scenario.id,
            "repetition": repetition,
            "phase": scenario.phase,
        }
    }
    lease = None
    runtime: ReleaseRuntime | None = None
    slot_acquired = False
    try:
        if scenario.phase == "decision":
            backend = ScenarioExecutionBackend(scenario)
        else:
            if settings.staging_resources is None:
                raise RuntimeError("staging resource manager is not configured")
            await staging_slots.acquire()
            slot_acquired = True
            lease = settings.staging_resources.prepare(settings.release_id, scenario)
            runtime_metadata.update(lease.runtime_metadata)
        runtime = settings.runtime_factory(scenario, backend, runtime_metadata)
        release_metadata = runtime_metadata.get("release_review", {})
        isolated_user_id = (
            release_metadata.get("namespace")
            if isinstance(release_metadata, dict)
            else None
        )
        state = await _arun_state(
            runtime,
            UserRequest(
                user_id=(
                    isolated_user_id
                    if isinstance(isolated_user_id, str) and isolated_user_id
                    else "release-review"
                ),
                session_id=f"{settings.release_id}-{scenario.id}-r{repetition}",
                text=scenario.request,
                assistant_mode=scenario.assistant_mode,
                metadata=runtime_metadata,
            ),
        )
        events = runtime.trace_store.list_by_run(state.run_id)
        evidence = ReleaseRunEvidence.from_state(state, events)
        _check_deadline(settings)
        response = getattr(getattr(state, "response", None), "message", None)
        output = {
            "scenario_id": scenario.id,
            "repetition": repetition,
            "phase": scenario.phase,
            "scenario_hash": scenario_hash(scenario),
            "response": response if isinstance(response, str) else "",
            "evidence": evidence.model_dump(mode="json"),
        }
        _progress(progress, "release_review.item.completed", item_key=key)
        return output
    finally:
        if runtime is not None and runtime.close() is False:
            raise RuntimeError(f"Release Review Runtime failed to close for {key}")
        if lease is not None:
            cleanup_results[key] = lease.cleanup()
        if slot_acquired:
            staging_slots.release()


async def _arun_state(runtime: Any, request: UserRequest) -> Any:
    function = getattr(runtime, "arun_state", None)
    if callable(function):
        return await function(request)
    sync_function = getattr(runtime, "run_state", None)
    if callable(sync_function):
        return await asyncio.to_thread(sync_function, request)
    raise RuntimeError("Release Review Runtime has no graph execution entry")


def _check_deadline(settings: ReleaseExperimentSettings) -> None:
    if (
        settings.deadline_monotonic is not None
        and monotonic() >= settings.deadline_monotonic
    ):
        raise TimeoutError("release review global deadline exceeded")


def _metadata(value: Any) -> dict[str, Any]:
    metadata = _field(value, "metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("Release Review Example metadata must be an object")
    return dict(metadata)


def _required_id(value: Any, label: str) -> str:
    result = str(_field(value, "id") or "")
    if not result:
        raise RuntimeError(f"{label} has no id")
    return result


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _current_run_tree() -> Any | None:
    from langsmith.run_helpers import get_current_run_tree

    return get_current_run_tree()


def _progress(callback: ProgressCallback | None, event: str, **details: object) -> None:
    if callback is not None:
        callback({"event": event, **details})


__all__ = [
    "ReleaseExperimentResult",
    "ReleaseExperimentSettings",
    "inspect_release_examples",
    "run_release_experiment",
]
