"""LangSmith Dataset and persisted-evidence boundary for Release Review."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
import math
import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from langsmith.utils import LangSmithNotFoundError, LangSmithRateLimitError

from evals.langsmith_feedback import normalize_boolean_feedback_score
from evals.langsmith_runtime_regression.experiment import audit_native_graph_tree

from .contracts import ReleaseScenario
from .loader import scenario_hash
from .report import CANONICAL_TASK_SCORES, ReleaseItemAssessment


RELEASE_REVIEW_DATASET = "assistant-agent-release-review"
GIT_EXAMPLE_OWNER = "assistant_agent_release_review"
REQUIRED_RELEASE_FEEDBACK_KEYS = CANONICAL_TASK_SCORES


@dataclass(frozen=True)
class ReleaseExampleBinding:
    example_id: str
    scenario_id: str
    repetition: int
    scenario_hash: str


@dataclass(frozen=True)
class LangSmithDatasetSyncResult:
    dataset_name: str
    dataset_id: str
    active_example_ids: tuple[str, ...]
    archived_example_ids: tuple[str, ...]
    bindings: tuple[ReleaseExampleBinding, ...]


@dataclass(frozen=True)
class ReleaseLangSmithCompletenessResult:
    """Persisted run and Feedback facts returned by the LangSmith API."""

    example_ids: tuple[str, ...]
    root_run_ids: tuple[str, ...]
    feedback: dict[str, dict[str, bool]]
    native_tree_complete: bool


def audit_langsmith_feedback(
    completeness: ReleaseLangSmithCompletenessResult,
    bindings: Collection[ReleaseExampleBinding],
    scenarios: Collection[ReleaseScenario],
    cleanup_results: dict[str, Any],
) -> tuple[ReleaseItemAssessment, ...]:
    """Project persisted Feedback into existing Release Review semantics."""

    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    binding_by_example = {
        binding.example_id: binding
        for binding in bindings
        if binding.scenario_id in scenario_by_id
    }
    if set(completeness.example_ids) != set(binding_by_example):
        raise RuntimeError(
            "Release Review persisted Examples do not match Git bindings"
        )
    if len(completeness.root_run_ids) != len(completeness.example_ids):
        raise RuntimeError("Release Review persisted root runs are incomplete")
    root_by_example = dict(
        zip(completeness.example_ids, completeness.root_run_ids, strict=True)
    )
    assessments: list[ReleaseItemAssessment] = []
    for example_id in completeness.example_ids:
        binding = binding_by_example[example_id]
        scenario = scenario_by_id[binding.scenario_id]
        key = f"{binding.scenario_id}:r{binding.repetition}"
        cleanup = cleanup_results.get(key)
        infrastructure_status = (
            getattr(cleanup, "infrastructure_status", None)
            if cleanup is not None
            else None
        )
        scores = completeness.feedback.get(example_id, {})
        if set(scores) != set(REQUIRED_RELEASE_FEEDBACK_KEYS):
            raise RuntimeError(f"Release Review Feedback incomplete for {example_id}")
        assessments.append(
            ReleaseItemAssessment(
                scenario_id=scenario.id,
                repetition=binding.repetition,
                phase=scenario.phase,
                risk=scenario.risk,
                scenario_hash=binding.scenario_hash,
                run_id=root_by_example[example_id],
                scores=dict(scores),
                infrastructure_status=infrastructure_status,
            )
        )
    return tuple(assessments)


def sync_langsmith_examples(
    client: Any,
    scenarios: Collection[ReleaseScenario],
    git_commit: str,
) -> LangSmithDatasetSyncResult:
    """Make Git scenarios authoritative over owned LangSmith Examples."""

    try:
        dataset = client.read_dataset(dataset_name=RELEASE_REVIEW_DATASET)
    except LangSmithNotFoundError:
        dataset = client.create_dataset(
            RELEASE_REVIEW_DATASET,
            description=(
                "Git-owned pre-release Agent Decision and Staging scenarios; "
                "LangSmith stores native graph Experiments and Feedback."
            ),
            metadata={"owner": GIT_EXAMPLE_OWNER, "kind": "release_review"},
        )
    dataset_id = _required_id(dataset, "Release Review Dataset")
    existing = list(client.list_examples(dataset_id=dataset_id))
    owned_by_key: dict[tuple[str, int], Any] = {}
    for example in existing:
        metadata = _metadata(example)
        if metadata.get("owner") != GIT_EXAMPLE_OWNER:
            continue
        key = _metadata_key(metadata)
        if key in owned_by_key:
            raise RuntimeError(f"duplicate Git-owned Release Review Example {key!r}")
        owned_by_key[key] = example

    active_ids: list[str] = []
    bindings: list[ReleaseExampleBinding] = []
    expected_keys: set[tuple[str, int]] = set()
    for scenario in sorted(scenarios, key=lambda item: item.id):
        digest = scenario_hash(scenario)
        for repetition in range(1, scenario.repetitions + 1):
            key = (scenario.id, repetition)
            expected_keys.add(key)
            inputs = {"scenario_id": scenario.id, "request": scenario.request}
            outputs = {
                "tool_contract": scenario.tool_contract.model_dump(
                    mode="json", exclude_none=True
                ),
                "state_assertions": [
                    assertion.model_dump(mode="json", exclude_none=True)
                    for assertion in scenario.state_assertions
                ],
            }
            metadata = {
                "owner": GIT_EXAMPLE_OWNER,
                "active": True,
                "scenario_id": scenario.id,
                "phase": scenario.phase,
                "capability": scenario.capability,
                "risk": scenario.risk,
                "scenario_hash": digest,
                "git_commit": git_commit,
                "repetition": repetition,
            }
            current = owned_by_key.get(key)
            if current is None:
                stable_id = _stable_example_id(scenario.id, repetition)
                created = client.create_example(
                    dataset_id=dataset_id,
                    example_id=stable_id,
                    inputs=inputs,
                    outputs=outputs,
                    metadata=metadata,
                )
                example_id = _required_id(created, "Release Review Example")
            else:
                example_id = _required_id(current, "Release Review Example")
                client.update_example(
                    example_id,
                    dataset_id=dataset_id,
                    inputs=inputs,
                    outputs=outputs,
                    metadata=metadata,
                )
            active_ids.append(example_id)
            bindings.append(
                ReleaseExampleBinding(
                    example_id=example_id,
                    scenario_id=scenario.id,
                    repetition=repetition,
                    scenario_hash=digest,
                )
            )

    archived: list[str] = []
    for key, example in owned_by_key.items():
        if key in expected_keys:
            continue
        example_id = _required_id(example, "Release Review Example")
        metadata = _metadata(example)
        metadata["active"] = False
        client.update_example(example_id, metadata=metadata)
        archived.append(example_id)
    return LangSmithDatasetSyncResult(
        dataset_name=RELEASE_REVIEW_DATASET,
        dataset_id=dataset_id,
        active_example_ids=tuple(active_ids),
        archived_example_ids=tuple(sorted(archived)),
        bindings=tuple(bindings),
    )


def wait_for_langsmith_runs(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ReleaseLangSmithCompletenessResult:
    """Wait for persisted native graph runs and all required Feedback."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("completeness timeout and poll interval must be positive")
    deadline = clock() + timeout_seconds
    attempts = math.ceil(timeout_seconds / poll_interval_seconds) + 1
    latest: dict[str, list[str]] = {}
    for attempt in range(attempts):
        if attempt and clock() >= deadline:
            break
        try:
            result, latest = _audit_remote_release_facts(
                client,
                experiment_id=experiment_id,
                example_ids=example_ids,
            )
        except LangSmithRateLimitError:
            result = None
            latest = {
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
    raise RuntimeError("LangSmith Release Review incomplete: " + repr(latest))


def _audit_remote_release_facts(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
) -> tuple[
    ReleaseLangSmithCompletenessResult | None,
    dict[str, list[str]],
]:
    # The SDK iterator owns pagination. Deliberately omit limit and time windows so
    # every persisted run in the exact Experiment project is considered.
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
    tree = audit_native_graph_tree(runs, example_ids=example_ids)
    problems = {
        example_id: list(values) for example_id, values in tree.problems.items()
    }
    root_to_example = {
        str(_field(run, "id")): str(_field(run, "reference_example_id") or "")
        for run in runs
        if _field(run, "parent_run_id") is None
        and str(_field(run, "reference_example_id") or "") in example_ids
    }
    feedback: dict[str, dict[str, bool]] = {
        example_id: {} for example_id in example_ids
    }
    if tree.run_ids:
        for item in client.list_feedback(run_ids=list(tree.run_ids)):
            example_id = root_to_example.get(str(_field(item, "run_id") or ""))
            key = str(_field(item, "key") or "")
            if example_id is None or key not in REQUIRED_RELEASE_FEEDBACK_KEYS:
                continue
            if key in feedback[example_id]:
                problems.setdefault(example_id, []).append(f"duplicate feedback {key}")
                continue
            score = _field(item, "score")
            try:
                normalized_score = normalize_boolean_feedback_score(score)
            except ValueError:
                problems.setdefault(example_id, []).append(f"invalid feedback {key}")
                continue
            feedback[example_id][key] = normalized_score
    required = set(REQUIRED_RELEASE_FEEDBACK_KEYS)
    for example_id in example_ids:
        missing = sorted(required - set(feedback[example_id]))
        if missing:
            problems.setdefault(example_id, []).append(
                "missing feedback " + repr(missing)
            )
    if problems:
        return None, problems
    return (
        ReleaseLangSmithCompletenessResult(
            example_ids=example_ids,
            root_run_ids=tree.run_ids,
            feedback=feedback,
            native_tree_complete=tree.complete,
        ),
        {},
    )


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _required_id(value: Any, label: str) -> str:
    identifier = str(_field(value, "id") or "")
    if not identifier:
        raise RuntimeError(f"{label} has no id")
    return identifier


def _metadata(example: Any) -> dict[str, Any]:
    value = _field(example, "metadata")
    if not isinstance(value, dict):
        raise RuntimeError("Release Review Example metadata must be an object")
    return dict(value)


def _metadata_key(metadata: dict[str, Any]) -> tuple[str, int]:
    scenario_id = metadata.get("scenario_id")
    repetition = metadata.get("repetition")
    if not isinstance(scenario_id, str) or not isinstance(repetition, int):
        raise RuntimeError("Git-owned Release Review Example metadata is invalid")
    return scenario_id, repetition


def _stable_example_id(scenario_id: str, repetition: int) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"assistant-agent:{RELEASE_REVIEW_DATASET}:{scenario_id}:r{repetition}",
    )


__all__ = [
    "GIT_EXAMPLE_OWNER",
    "LangSmithDatasetSyncResult",
    "REQUIRED_RELEASE_FEEDBACK_KEYS",
    "RELEASE_REVIEW_DATASET",
    "ReleaseExampleBinding",
    "ReleaseLangSmithCompletenessResult",
    "audit_langsmith_feedback",
    "sync_langsmith_examples",
    "wait_for_langsmith_runs",
]
