"""Pure product projection for native Durable Workflow graph facts.

The projector is intentionally stateless: it never advances the graph, reads a
business repository, or exposes LangGraph checkpoint/task identifiers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.workflows.durable_graph_app import WorkflowGraphStreamPart
from assistant_agent.workflows.graph_state import (
    DurableWorkflowState,
    PersistedAdmittedWorkflowPlan,
    WorkflowGraphError,
    WorkflowGraphStatus,
    WorkflowPhase,
    WorkflowProfileAssignment,
    latest_results,
    stable_workflow_action_ref,
    validate_durable_workflow_state,
)


ProductEventType = Literal[
    "accepted",
    "planning",
    "progress",
    "waiting_input",
    "completed",
    "cancelled",
    "failed",
]


class _StrictProductModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkflowHandle(_StrictProductModel):
    workflow_id: str = Field(min_length=1, max_length=512)
    workflow_type: Literal["deep_research"]
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    output_ref: str = Field(pattern=r"^workflow://[^/\s]+$")

    @model_validator(mode="after")
    def validate_output_ref(self) -> "WorkflowHandle":
        if self.output_ref != f"workflow://{self.workflow_id}":
            raise ValueError("workflow output reference does not match handle")
        return self


class WorkflowActiveItem(_StrictProductModel):
    node_id: str = Field(min_length=1, max_length=120)
    display_title: str = Field(min_length=1, max_length=160)
    status: Literal["running", "blocked"]
    execution_generation: int = Field(ge=0, le=64)


class WorkflowProductProgress(_StrictProductModel):
    state: Literal[
        "planning", "working", "waiting_input", "completed", "failed"
    ]
    phase: WorkflowPhase
    completed_items: int = Field(ge=0, le=256)
    total_items: int = Field(ge=0, le=256)
    active_items: tuple[WorkflowActiveItem, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_counts(self) -> "WorkflowProductProgress":
        if self.completed_items > self.total_items:
            raise ValueError("completed item count exceeds total item count")
        return self


class WorkflowWaitingAction(_StrictProductModel):
    action_ref: str = Field(min_length=1, max_length=512)
    node_id: str = Field(min_length=1, max_length=120)
    required_fields: tuple[str, ...] = Field(min_length=1, max_length=32)
    prompt_code: str = Field(min_length=1, max_length=160)
    safe_prompt: str = Field(min_length=1, max_length=2_000)


class WorkflowProductSnapshot(_StrictProductModel):
    handle: WorkflowHandle
    progress: WorkflowProductProgress
    result_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    waiting_actions: tuple[WorkflowWaitingAction, ...] = Field(default=(), max_length=256)
    terminal_reason_code: str | None = Field(default=None, max_length=160)


class WorkflowProductEvent(_StrictProductModel):
    schema_version: Literal["workflow_product_event_v1"] = (
        "workflow_product_event_v1"
    )
    event_id: str = Field(pattern=r"^workflow-event:sha256:[0-9a-f]{64}$")
    workflow_id: str = Field(min_length=1, max_length=512)
    event_type: ProductEventType
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    progress: WorkflowProductProgress
    result_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    waiting_actions: tuple[WorkflowWaitingAction, ...] = Field(default=(), max_length=256)
    terminal_reason_code: str | None = Field(default=None, max_length=160)


class WorkflowGraphProjector:
    """Map authoritative graph state to narrow product DTOs."""

    def project_stream_part(
        self, part: WorkflowGraphStreamPart
    ) -> WorkflowProductEvent | None:
        # Native values/updates/tasks/checkpoints contain execution internals.
        # Stream projection accepts only an explicit, strict product custom fact.
        if part.type != "custom" or not isinstance(part.data, Mapping):
            return None
        try:
            return WorkflowProductEvent.model_validate(part.data)
        except ValueError:
            return None

    def project_snapshot(
        self, state: Mapping[str, object]
    ) -> WorkflowProductSnapshot:
        checked = validate_durable_workflow_state(state)
        return WorkflowProductSnapshot(
            handle=_handle(checked),
            progress=_progress(checked),
            result_artifact_refs=tuple(checked["result_artifact_refs"]),
            waiting_actions=_waiting_actions(checked),
            terminal_reason_code=_terminal_reason_code(checked),
        )

    def project_event(
        self, state: Mapping[str, object]
    ) -> WorkflowProductEvent:
        snapshot = self.project_snapshot(state)
        event_type = _event_type(snapshot)
        event_fact = {
            "workflow_id": snapshot.handle.workflow_id,
            "event_type": event_type,
            "status": snapshot.handle.status,
            "phase": snapshot.handle.phase,
            "progress": snapshot.progress.model_dump(mode="json"),
            "result_artifact_refs": list(snapshot.result_artifact_refs),
            "waiting_actions": [
                item.model_dump(mode="json") for item in snapshot.waiting_actions
            ],
            "terminal_reason_code": snapshot.terminal_reason_code,
        }
        digest = hashlib.sha256(
            json.dumps(
                event_fact,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return WorkflowProductEvent(
            event_id=f"workflow-event:sha256:{digest}",
            workflow_id=snapshot.handle.workflow_id,
            event_type=event_type,
            status=snapshot.handle.status,
            phase=snapshot.handle.phase,
            progress=snapshot.progress,
            result_artifact_refs=snapshot.result_artifact_refs,
            waiting_actions=snapshot.waiting_actions,
            terminal_reason_code=snapshot.terminal_reason_code,
        )


def _handle(state: DurableWorkflowState) -> WorkflowHandle:
    workflow_id = state["workflow_id"]
    return WorkflowHandle(
        workflow_id=workflow_id,
        workflow_type="deep_research",
        status=state["status"],
        phase=state["phase"],
        output_ref=f"workflow://{workflow_id}",
    )


def _progress(state: DurableWorkflowState) -> WorkflowProductProgress:
    plan = state["admitted_plan"]
    if plan is None:
        return WorkflowProductProgress(
            state="planning",
            phase=state["phase"],
            completed_items=0,
            total_items=0,
            active_items=(),
        )
    checked_plan = PersistedAdmittedWorkflowPlan.model_validate_json(json.dumps(plan))
    nodes = {node.node_id: node for node in checked_plan.nodes}
    results = latest_results(
        state["result_ledger"], state["execution_generation_by_node"]
    )
    completed = sum(result.status == "succeeded" for result in results.values())
    active: list[WorkflowActiveItem] = []
    for raw_assignment in state["active_wave"]:
        assignment = WorkflowProfileAssignment.model_validate_json(
            json.dumps(raw_assignment)
        )
        result = results.get(assignment.node_id)
        status: Literal["running", "blocked"] = (
            "blocked" if result is not None and result.status == "blocked" else "running"
        )
        node = nodes[assignment.node_id]
        active.append(
            WorkflowActiveItem(
                node_id=node.node_id,
                display_title=node.display_title,
                status=status,
                execution_generation=assignment.execution_generation,
            )
        )
    product_state: Literal[
        "planning", "working", "waiting_input", "completed", "failed"
    ] = (
        "completed"
        if state["status"] == "completed"
        else "waiting_input"
        if state["status"] == "waiting_input"
        else "failed"
        if state["status"] in {"failed", "cancelled", "blocked"}
        else "working"
    )
    return WorkflowProductProgress(
        state=product_state,
        phase=state["phase"],
        completed_items=completed,
        total_items=len(nodes),
        active_items=tuple(sorted(active, key=lambda item: item.node_id)),
    )


def _waiting_actions(
    state: DurableWorkflowState,
) -> tuple[WorkflowWaitingAction, ...]:
    results = latest_results(
        state["result_ledger"], state["execution_generation_by_node"]
    )
    values = []
    for node_id, result in results.items():
        request = result.input_request
        if result.status != "blocked" or request is None:
            continue
        values.append(
            WorkflowWaitingAction(
                action_ref=stable_workflow_action_ref(
                    workflow_id=state["workflow_id"],
                    node_id=node_id,
                    execution_generation=result.execution_generation,
                ),
                node_id=node_id,
                required_fields=request.required_fields,
                prompt_code=request.prompt_code,
                safe_prompt=request.safe_prompt,
            )
        )
    return tuple(sorted(values, key=lambda item: item.action_ref))


def _terminal_reason_code(state: DurableWorkflowState) -> str | None:
    if state["status"] not in {"failed", "cancelled", "blocked"}:
        return None
    return (
        WorkflowGraphError.model_validate_json(json.dumps(state["errors"][0])).code
        if state["errors"]
        else "workflow_failed"
    )


def _event_type(snapshot: WorkflowProductSnapshot) -> ProductEventType:
    if snapshot.handle.status == "completed":
        return "completed"
    if snapshot.handle.status == "cancelled":
        return "cancelled"
    if snapshot.handle.status in {"failed", "blocked"}:
        return "failed"
    if snapshot.handle.status == "waiting_input" or snapshot.waiting_actions:
        return "waiting_input"
    if snapshot.handle.phase == "planning":
        return "planning"
    if snapshot.handle.status == "queued":
        return "accepted"
    return "progress"


__all__ = [
    "WorkflowActiveItem",
    "WorkflowGraphProjector",
    "WorkflowHandle",
    "WorkflowProductEvent",
    "WorkflowProductProgress",
    "WorkflowProductSnapshot",
    "WorkflowWaitingAction",
]
