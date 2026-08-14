"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, NotRequired, Required, TypedDict

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict

from assistant_agent.native_agent.models import (
    PlanningArtifact,
    VerificationResult,
    WorkerResult,
)
from assistant_agent.workflows.models import WorkflowPlanV2Proposal


ExecutionMode = Literal["fast", "planning"]
MemoryStatus = Literal["ready", "empty", "degraded"]


class AssistantRootInput(BaseModel):
    """Strict public input for a new native assistant run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]
    execution_mode: ExecutionMode


class FastAgentState(AgentState):
    """State consumed by the reusable create_agent subgraph."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]


class AssistantRootState(FastAgentState):
    """Minimal state shared across the parent graph's two branches."""

    execution_mode: Required[ExecutionMode]


class WorkerState(FastAgentState):
    """Narrow input/output state for one planning worker branch."""

    work_item_id: Required[str]
    objective: Required[str]
    revision: NotRequired[int]


def merge_worker_results(
    left: Mapping[str, WorkerResult | Mapping[str, object]] | None,
    right: Mapping[str, WorkerResult | Mapping[str, object]] | None,
) -> dict[str, WorkerResult]:
    """Merge parallel worker results without allowing stable-ID overwrite."""

    return _merge_keyed_models(
        left,
        right,
        model_type=WorkerResult,
        identity_field="work_item_id",
        conflict_label="worker result",
    )


def merge_artifacts(
    left: Mapping[str, PlanningArtifact | Mapping[str, object]] | None,
    right: Mapping[str, PlanningArtifact | Mapping[str, object]] | None,
) -> dict[str, PlanningArtifact]:
    """Merge artifacts by stable ID and reject conflicting contents."""

    return _merge_keyed_models(
        left,
        right,
        model_type=PlanningArtifact,
        identity_field="artifact_id",
        conflict_label="artifact",
    )


def merge_sorted_ids(
    left: tuple[str, ...] | list[str] | None,
    right: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    """Return deterministic set-union serialization for completed work IDs."""

    return tuple(sorted({*(left or ()), *(right or ())}))


def _merge_keyed_models(left, right, *, model_type, identity_field, conflict_label):
    merged = _normalize_keyed_models(left, model_type, identity_field)
    for key, candidate in _normalize_keyed_models(
        right, model_type, identity_field
    ).items():
        existing = merged.get(key)
        if existing is not None and existing != candidate:
            existing_revision = getattr(existing, "revision", None)
            candidate_revision = getattr(candidate, "revision", None)
            if (
                existing_revision is None
                or candidate_revision is None
                or existing_revision == candidate_revision
            ):
                raise ValueError(f"{conflict_label} conflict: {key}")
            if candidate_revision < existing_revision:
                continue
        merged[key] = candidate
    return merged


def _normalize_keyed_models(values, model_type, identity_field):
    normalized = {}
    for key, value in (values or {}).items():
        item = value if isinstance(value, model_type) else model_type.model_validate(value)
        identity = getattr(item, identity_field)
        if key != identity:
            raise ValueError(f"mapping key does not match {identity_field}: {key}")
        normalized[key] = item
    return normalized


class PlanningState(AgentState):
    """Planning-only channels kept out of the fast branch."""

    memory_context: Required[tuple[str, ...]]
    plan: NotRequired[WorkflowPlanV2Proposal]
    worker_results: NotRequired[
        Annotated[dict[str, WorkerResult], merge_worker_results]
    ]
    completed_work_item_ids: NotRequired[
        Annotated[tuple[str, ...], merge_sorted_ids]
    ]
    artifacts: NotRequired[Annotated[dict[str, PlanningArtifact], merge_artifacts]]
    verification: NotRequired[VerificationResult]
    repair_count: NotRequired[int]


__all__ = [
    "AssistantRootInput",
    "AssistantRootState",
    "ExecutionMode",
    "FastAgentState",
    "MemoryStatus",
    "PlanningState",
    "WorkerState",
    "merge_artifacts",
    "merge_sorted_ids",
    "merge_worker_results",
]
