from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Any, Protocol

from langfuse import Evaluation

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.tool_execution_backend import ToolExecutionBackend

from .assertions import ConformanceResult, evaluate_task_conformance
from .contracts import ReleaseScenario
from .decision_backend import ScenarioExecutionBackend
from .evidence import ReleaseRunEvidence
from .loader import scenario_hash
from .staging import CleanupResult, StagingResourceManager
from .sync_dataset import RELEASE_REVIEW_DATASET


QUALITY_SCORE_PREFIX = "assistant_agent.quality."


class ReleaseRuntime(Protocol):
    trace_store: Any

    def run_state(self, request: UserRequest) -> Any: ...

    def close(self) -> bool: ...


RuntimeFactory = Callable[
    [ReleaseScenario, ToolExecutionBackend | None, Mapping[str, Any]], ReleaseRuntime
]
ProgressCallback = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class ReleaseExperimentSettings:
    release_id: str
    model: str
    prompt_version: str
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
    run_name: str
    dataset_run_id: str | None
    dataset_run_url: str | None
    cleanup_results: dict[str, CleanupResult] = field(default_factory=dict)
    expected_item_keys: tuple[str, ...] = ()


def run_release_experiment(
    client: Any,
    scenarios: Collection[ReleaseScenario],
    settings: ReleaseExperimentSettings,
    progress: ProgressCallback | None = None,
) -> ReleaseExperimentResult:
    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    if len(scenario_by_id) != len(scenarios):
        raise ValueError("release scenarios must have unique ids")
    dataset = client.get_dataset(RELEASE_REVIEW_DATASET)
    selected_items = [
        item
        for item in dataset.items
        if _item_status(item) == "ACTIVE"
        and _item_metadata(item).get("scenario_id") in scenario_by_id
    ]
    expected_keys = tuple(
        f"{scenario.id}:r{repetition}"
        for scenario in sorted(scenarios, key=lambda item: item.id)
        for repetition in range(1, scenario.repetitions + 1)
    )
    actual_keys = tuple(sorted(_item_key(item) for item in selected_items))
    if tuple(sorted(expected_keys)) != actual_keys:
        raise RuntimeError(
            "release Dataset items do not match Git scenarios: "
            f"expected={sorted(expected_keys)}, actual={list(actual_keys)}"
        )
    selected_dataset = _selected_dataset(dataset, selected_items)
    cleanup_results: dict[str, CleanupResult] = {}
    cleanup_lock = Lock()
    staging_slots = BoundedSemaphore(settings.staging_concurrency)
    evaluator_failures: list[Exception] = []

    def execute_item(*, item: Any, **_: Any) -> dict[str, Any]:
        _check_deadline(settings)
        metadata = _item_metadata(item)
        item_input = _item_field(item, "input")
        scenario_id = metadata.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id not in scenario_by_id:
            raise RuntimeError(f"unexpected release scenario id: {scenario_id!r}")
        scenario = scenario_by_id[scenario_id]
        if metadata.get("scenario_hash") != scenario_hash(scenario):
            raise RuntimeError(f"scenario hash mismatch for {scenario_id}")
        if not isinstance(item_input, dict) or item_input.get("request") != scenario.request:
            raise RuntimeError(f"Dataset request mismatch for {scenario_id}")
        repetition = metadata.get("repetition")
        if not isinstance(repetition, int):
            raise RuntimeError(f"Dataset repetition is invalid for {scenario_id}")
        key = f"{scenario_id}:r{repetition}"
        _progress(progress, "release_review.item.started", item_key=key)
        backend: ToolExecutionBackend | None = None
        runtime_metadata: dict[str, Any] = {
            "release_review": {
                "release_id": settings.release_id,
                "scenario_id": scenario_id,
                "repetition": repetition,
                "phase": scenario.phase,
            }
        }
        lease = None
        slot_acquired = False
        runtime: ReleaseRuntime | None = None
        try:
            if scenario.phase == "decision":
                backend = ScenarioExecutionBackend(scenario)
            else:
                if settings.staging_resources is None:
                    raise RuntimeError("staging resource manager is not configured")
                staging_slots.acquire()
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
            request = UserRequest(
                user_id=(
                    isolated_user_id
                    if isinstance(isolated_user_id, str) and isolated_user_id
                    else "release-review"
                ),
                session_id=f"{settings.release_id}-{scenario_id}-r{repetition}",
                text=scenario.request,
                metadata=runtime_metadata,
            )
            state = runtime.run_state(request)
            trace_events = runtime.trace_store.list_by_run(state.run_id)
            evidence = ReleaseRunEvidence.from_state(state, trace_events)
            _check_deadline(settings)
            output = {
                "scenario_id": scenario_id,
                "repetition": repetition,
                "phase": scenario.phase,
                "scenario_hash": scenario_hash(scenario),
                "evidence": evidence.model_dump(mode="json"),
            }
            _progress(progress, "release_review.item.completed", item_key=key)
            return output
        finally:
            if runtime is not None:
                runtime.close()
            if lease is not None:
                cleanup = lease.cleanup()
                with cleanup_lock:
                    cleanup_results[key] = cleanup
            if slot_acquired:
                staging_slots.release()

    def evaluate_item(*, output: Any, metadata: dict[str, Any] | None = None, **_: Any):
        try:
            scenario_id = (metadata or {}).get("scenario_id")
            scenario = scenario_by_id.get(str(scenario_id))
            if scenario is None:
                raise RuntimeError(f"evaluator received unknown scenario: {scenario_id!r}")
            if not isinstance(output, dict) or not isinstance(output.get("evidence"), dict):
                raise RuntimeError("release Experiment output is missing evidence")
            conformance = evaluate_task_conformance(
                scenario,
                ReleaseRunEvidence.model_validate(output["evidence"]),
            )
            if conformance.passed is None:
                raise RuntimeError("infrastructure failure cannot produce a quality Score")
            return [_task_conformance_evaluation(conformance, scenario, settings)]
        except Exception as exc:
            evaluator_failures.append(exc)
            raise

    native = selected_dataset.run_experiment(
        name="assistant-agent-release-review",
        run_name=settings.run_name,
        description=(
            "Pre-release Agent review using deterministic Decision fixtures and "
            "isolated real Staging resources."
        ),
        task=execute_item,
        evaluators=[evaluate_item],
        max_concurrency=settings.max_concurrency,
        metadata={
            "evaluation_mode": "release_review",
            "release_id": settings.release_id,
            "model": settings.model,
            "prompt_version": settings.prompt_version,
            "git_commit": settings.git_commit,
            "catalog_generation": settings.catalog_generation,
            "evaluator_version": settings.evaluator_version,
        },
    )
    if evaluator_failures:
        raise RuntimeError(
            f"release review evaluator failed: {evaluator_failures[0]}"
        ) from evaluator_failures[0]
    return ReleaseExperimentResult(
        native_result=native,
        run_name=native.run_name,
        dataset_run_id=getattr(native, "dataset_run_id", None),
        dataset_run_url=getattr(native, "dataset_run_url", None),
        cleanup_results=dict(cleanup_results),
        expected_item_keys=expected_keys,
    )


def _task_conformance_evaluation(
    result: ConformanceResult,
    scenario: ReleaseScenario,
    settings: ReleaseExperimentSettings,
) -> Evaluation:
    failed = [item for item in result.assertions if not item.passed]
    visible = failed or list(result.assertions)
    comment = "; ".join(
        f"{item.label}: {item.reason}" if failed else item.label for item in visible
    )
    metadata: dict[str, Any] = {
        "evaluation_mode": "release_review",
        "phase": scenario.phase,
        "release_id": settings.release_id,
        "evaluator_version": settings.evaluator_version,
    }
    for assertion in result.assertions:
        prefix = f"assertion.{assertion.key}"
        metadata[f"{prefix}.passed"] = assertion.passed
        metadata[f"{prefix}.label"] = assertion.label
        metadata[f"{prefix}.method"] = "rule"
    return Evaluation(
        name=f"{QUALITY_SCORE_PREFIX}task_conformance",
        value=bool(result.passed),
        data_type="BOOLEAN",
        comment=comment,
        metadata=metadata,
    )


def _selected_dataset(dataset: Any, items: list[Any]) -> Any:
    if len(items) == len(getattr(dataset, "items", ())):
        return dataset
    try:
        from copy import copy

        selected = copy(dataset)
        selected.items = items
        return selected
    except Exception:
        dataset.items = items
        return dataset


def _check_deadline(settings: ReleaseExperimentSettings) -> None:
    if settings.deadline_monotonic is not None and monotonic() >= settings.deadline_monotonic:
        raise TimeoutError("release review global deadline exceeded")


def _item_key(item: Any) -> str:
    metadata = _item_metadata(item)
    return f"{metadata.get('scenario_id')}:r{metadata.get('repetition')}"


def _item_metadata(item: Any) -> dict[str, Any]:
    metadata = _item_field(item, "metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("release Dataset item metadata must be an object")
    return metadata


def _item_status(item: Any) -> Any:
    status = _item_field(item, "status")
    return getattr(status, "value", status)


def _item_field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _progress(callback: ProgressCallback | None, event: str, **details: object) -> None:
    if callback is not None:
        callback({"event": event, **details})
