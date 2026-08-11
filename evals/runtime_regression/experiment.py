from __future__ import annotations

from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any, Protocol

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.evaluation.experiment_trace import (
    wait_for_experiment_trace_completeness,
)
from evals.release_review.evidence import ReleaseRunEvidence

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.runtime_regression_contract import (
    assistant_output,
    request_text,
    validate_failure_baseline,
)


class RuntimeRegressionRuntime(Protocol):
    trace_store: Any

    def run_state(self, request: UserRequest) -> Any: ...

    def close(self) -> bool: ...


RuntimeFactory = Callable[[], RuntimeRegressionRuntime]
GROUNDING_EXPERIMENT_SCORE_NAME = "assistant_agent.quality.grounding.experiment"
REQUIRED_EXPERIMENT_SCORE_NAMES = (
    "assistant_agent.quality.response_quality.experiment",
    GROUNDING_EXPERIMENT_SCORE_NAME,
    "assistant_agent.quality.regression_improvement.experiment",
)


@dataclass(frozen=True)
class RuntimeRegressionExperimentSettings:
    model: str
    runtime_factory: RuntimeFactory
    run_name: str
    max_concurrency: int = 1


@dataclass(frozen=True)
class RuntimeRegressionExperimentResult:
    native_result: Any
    run_name: str
    dataset_run_id: str | None
    dataset_run_url: str | None
    dataset_item_ids: tuple[str, ...]


def run_runtime_regression_experiment(
    client: Any,
    settings: RuntimeRegressionExperimentSettings,
) -> RuntimeRegressionExperimentResult:
    """Replay active production-derived cases through the production runtime."""

    dataset, items = inspect_runtime_regression_dataset(client)
    selected_dataset = dataset
    if len(items) != len(getattr(dataset, "items", ())):
        selected_dataset = copy(dataset)
        selected_dataset.items = items
    item_ids = tuple(_require_item_id(item) for item in items)

    def execute_item(*, item: Any, **_: Any) -> dict[str, Any]:
        item_id = _require_item_id(item)
        item_input = _item_field(item, "input")
        if not isinstance(item_input, dict):
            raise RuntimeError(f"runtime regression item {item_id!r} input must be an object")
        item_request_text = request_text(item_id, item_input)
        # Experiment item input is projected from the canonical task observation.
        client.update_current_span(input=item_input)
        runtime = settings.runtime_factory()
        try:
            state = runtime.run_state(
                UserRequest(
                    user_id="runtime-regression",
                    session_id=f"runtime-regression-{item_id}",
                    text=item_request_text,
                    metadata={
                        "runtime_regression": {"dataset_item_id": item_id}
                    },
                )
            )
            events = runtime.trace_store.list_by_run(state.run_id)
            evidence = ReleaseRunEvidence.from_state(state, events)
            output = assistant_output(state)
            with client.start_as_current_observation(
                name="runtime-regression-evidence",
                as_type="span",
                input=evidence.model_dump(mode="json"),
            ) as evidence_span:
                evidence_span.update(output=output)
            return output
        finally:
            runtime.close()

    native = selected_dataset.run_experiment(
        name="assistant-agent-runtime-regression",
        run_name=settings.run_name,
        description=(
            "Production-runtime replay of human-reviewed failures added to the "
            "fixed Dataset in the Langfuse UI."
        ),
        task=execute_item,
        evaluators=[],
        max_concurrency=settings.max_concurrency,
        metadata={
            "evaluation_mode": "runtime_regression",
            "model": settings.model,
        },
    )
    return RuntimeRegressionExperimentResult(
        native_result=native,
        run_name=native.run_name,
        dataset_run_id=getattr(native, "dataset_run_id", None),
        dataset_run_url=getattr(native, "dataset_run_url", None),
        dataset_item_ids=item_ids,
    )


def inspect_runtime_regression_dataset(client: Any) -> tuple[Any, list[Any]]:
    """Load and validate active Langfuse-owned regression items without running them."""

    dataset = client.get_dataset(RUNTIME_REGRESSION_DATASET)
    items = sorted(
        (
            item
            for item in getattr(dataset, "items", ())
            if _item_status(item) == "ACTIVE"
        ),
        key=lambda item: str(_item_field(item, "id")),
    )
    if not items:
        raise RuntimeError("runtime regression Dataset has no active items")
    for item in items:
        item_id = _require_item_id(item)
        item_input = _item_field(item, "input")
        if not isinstance(item_input, dict):
            raise RuntimeError(f"runtime regression item {item_id!r} input must be an object")
        request_text(item_id, item_input)
        validate_failure_baseline(item_id, _item_field(item, "expected_output"))
    return dataset, items


def wait_for_runtime_regression_scores(
    client: Any,
    *,
    experiment_id: str,
    dataset_item_ids: tuple[str, ...],
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    """Wait for every async Langfuse Experiment Rule Score or fail closed."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("Score wait timeout and poll interval must be positive")
    required = set(REQUIRED_EXPERIMENT_SCORE_NAMES)
    expected_items = set(dataset_item_ids)
    attempts = math.floor(timeout_seconds / poll_interval_seconds) + 1
    from_start_time = datetime.now(timezone.utc) - timedelta(days=1)
    latest: dict[str, dict[str, Any]] = {}
    for attempt in range(attempts):
        response = client.api.experiments.list_items(
            from_start_time=from_start_time,
            experiment_id=experiment_id,
            fields="core,scores",
            score_limit=50,
            limit=max(1, len(dataset_item_ids)),
        )
        latest = {}
        for item in getattr(response, "data", ()):
            item_id = _item_field(item, "experiment_item_id")
            if item_id not in expected_items:
                continue
            latest[item_id] = {
                _item_field(score, "name"): _item_field(score, "value")
                for score in _item_field(item, "scores") or ()
                if _item_field(score, "name") in required
            }
            if GROUNDING_EXPERIMENT_SCORE_NAME not in latest[item_id]:
                trace_id = _item_field(item, "trace_id")
                grounding_score = _evidence_grounding_score(client, trace_id)
                if grounding_score is not None:
                    latest[item_id][GROUNDING_EXPERIMENT_SCORE_NAME] = grounding_score
        if all(required <= set(latest.get(item_id, {})) for item_id in expected_items):
            return latest
        if attempt + 1 < attempts:
            sleep(poll_interval_seconds)
    missing = {
        item_id: sorted(required - set(latest.get(item_id, {})))
        for item_id in sorted(expected_items)
        if not required <= set(latest.get(item_id, {}))
    }
    raise RuntimeError(
        "Langfuse Experiment Score completeness timeout; missing=" + repr(missing)
    )


def _evidence_grounding_score(client: Any, trace_id: Any) -> Any | None:
    if not isinstance(trace_id, str) or not trace_id:
        return None
    observations = client.api.observations.get_many(
        trace_id=trace_id,
        name="runtime-regression-evidence",
        type="SPAN",
        fields="core",
        limit=2,
    ).data
    if len(observations) != 1:
        return None
    observation_id = _item_field(observations[0], "id")
    if not isinstance(observation_id, str) or not observation_id:
        return None
    scores = client.api.scores_v3.get_many_v3(
        name=GROUNDING_EXPERIMENT_SCORE_NAME,
        trace_id=trace_id,
        fields="subject",
        limit=100,
    ).data
    matching = [
        score
        for score in scores
        if _item_field(score, "name") == GROUNDING_EXPERIMENT_SCORE_NAME
        and _item_field(_item_field(score, "subject"), "kind") == "observation"
        and _item_field(_item_field(score, "subject"), "id") == observation_id
    ]
    if len(matching) != 1:
        return None
    return _item_field(matching[0], "value")


def wait_for_runtime_regression_trace_completeness(
    client: Any,
    *,
    experiment_id: str,
    dataset_item_ids: tuple[str, ...],
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    return wait_for_experiment_trace_completeness(
        client,
        experiment_id=experiment_id,
        experiment_item_ids=dataset_item_ids,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )


def _require_item_id(item: Any) -> str:
    item_id = _item_field(item, "id")
    if not isinstance(item_id, str) or not item_id:
        raise RuntimeError("runtime regression Dataset item has no id")
    return item_id


def _item_status(item: Any) -> Any:
    status = _item_field(item, "status")
    return getattr(status, "value", status)


def _item_field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)
