"""Pure product projection for native Durable Workflow graph facts.

The projector is intentionally stateless: it never advances the graph, reads a
business repository, or exposes LangGraph checkpoint/task identifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant_agent.workflows.durable_graph_app import WorkflowGraphStreamPart
from assistant_agent.workflows.graph_state import (
    DurableWorkflowState,
    PersistedAdmittedWorkflowPlan,
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
_ARTIFACT_REF_PATTERN = re.compile(
    r"^(?:artifact|workflow-artifact|mock)://[A-Za-z0-9][A-Za-z0-9._~:/-]{0,1014}$"
)
_ACTION_REF_PATTERN = re.compile(
    r"^workflow:(?P<workflow_id>[^:\s]{1,512}):node:"
    r"(?P<node_id>[a-zA-Z][a-zA-Z0-9_.-]{0,119}):generation:"
    r"(?P<generation>0|[1-9][0-9]?)$"
)
_UNSAFE_PROMPT_FRAGMENTS = (
    "/home/",
    "/root/",
    "file://",
    "api_key",
    "access_token",
    "password=",
)


class _StrictProductModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkflowHandle(_StrictProductModel):
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$")
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
    action_ref: str = Field(pattern=_ACTION_REF_PATTERN.pattern)
    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    required_fields: tuple[str, ...] = Field(min_length=1, max_length=32)
    prompt_code: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,159}$")
    safe_prompt: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_action(self) -> "WorkflowWaitingAction":
        match = _ACTION_REF_PATTERN.fullmatch(self.action_ref)
        if match is None or match.group("node_id") != self.node_id:
            raise ValueError("workflow waiting action identity mismatch")
        if int(match.group("generation")) > 64:
            raise ValueError("workflow waiting action generation is out of range")
        if (
            len(self.required_fields) != len(set(self.required_fields))
            or any(not value.strip() or len(value) > 160 for value in self.required_fields)
        ):
            raise ValueError("workflow waiting fields must be bounded and unique")
        prompt = self.safe_prompt.casefold()
        if any(fragment in prompt for fragment in _UNSAFE_PROMPT_FRAGMENTS):
            raise ValueError("workflow waiting prompt contains unsafe runtime detail")
        return self


class WorkflowProductSnapshot(_StrictProductModel):
    handle: WorkflowHandle
    progress: WorkflowProductProgress
    result_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    waiting_actions: tuple[WorkflowWaitingAction, ...] = Field(default=(), max_length=256)
    terminal_reason_code: str | None = Field(
        default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,159}$"
    )

    @field_validator("result_artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _ARTIFACT_REF_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("product artifact refs must be unique bounded opaque URIs")
        return value

    @model_validator(mode="after")
    def validate_product_state(self) -> "WorkflowProductSnapshot":
        handle = self.handle
        progress = self.progress
        if progress.phase != handle.phase:
            raise ValueError("product progress phase does not match workflow handle")
        terminal_pairs = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        if handle.status in terminal_pairs:
            if handle.phase != terminal_pairs[handle.status]:
                raise ValueError("terminal workflow status and phase are inconsistent")
        elif handle.phase in set(terminal_pairs.values()):
            raise ValueError("non-terminal workflow cannot use a terminal phase")
        for action in self.waiting_actions:
            match = _ACTION_REF_PATTERN.fullmatch(action.action_ref)
            if match is None or match.group("workflow_id") != handle.workflow_id:
                raise ValueError("waiting action belongs to another workflow")
        waiting = handle.phase == "waiting_input" or handle.status in {
            "waiting_input",
            "blocked",
        }
        if waiting:
            if (
                progress.state != "waiting_input"
                or not self.waiting_actions
                or self.terminal_reason_code is not None
            ):
                raise ValueError("waiting workflow requires resumable product actions")
        elif self.waiting_actions:
            raise ValueError("non-waiting workflow cannot expose waiting actions")
        expected_progress = (
            "completed"
            if handle.status == "completed" and handle.phase == "completed"
            else "failed"
            if handle.status in {"failed", "cancelled"}
            and handle.phase in {"failed", "cancelled"}
            else "planning"
            if handle.phase == "planning"
            else "working"
        )
        if not waiting and progress.state != expected_progress:
            raise ValueError("product progress state is inconsistent with workflow status")
        terminal = handle.status in {"completed", "failed", "cancelled"}
        if handle.status in {"failed", "cancelled"}:
            if self.terminal_reason_code is None:
                raise ValueError("failed product snapshot requires a safe reason code")
        elif self.terminal_reason_code is not None:
            raise ValueError("non-failed product snapshot cannot expose a terminal reason")
        if terminal and progress.active_items:
            raise ValueError("terminal product snapshot cannot have active items")
        return self


class WorkflowProductEvent(_StrictProductModel):
    schema_version: Literal["workflow_product_event_v1"] = (
        "workflow_product_event_v1"
    )
    event_id: str = Field(pattern=r"^workflow-event:sha256:[0-9a-f]{64}$")
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$")
    event_type: ProductEventType
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    progress: WorkflowProductProgress
    result_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    waiting_actions: tuple[WorkflowWaitingAction, ...] = Field(default=(), max_length=256)
    terminal_reason_code: str | None = Field(
        default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,159}$"
    )

    @model_validator(mode="after")
    def validate_event(self) -> "WorkflowProductEvent":
        snapshot = WorkflowProductSnapshot(
            handle=WorkflowHandle(
                workflow_id=self.workflow_id,
                workflow_type="deep_research",
                status=self.status,
                phase=self.phase,
                output_ref=f"workflow://{self.workflow_id}",
            ),
            progress=self.progress,
            result_artifact_refs=self.result_artifact_refs,
            waiting_actions=self.waiting_actions,
            terminal_reason_code=self.terminal_reason_code,
        )
        if self.event_type != _event_type(snapshot):
            raise ValueError("product event type is inconsistent with workflow state")
        if self.event_id != _event_id_for_fact(_event_fact(snapshot, self.event_type)):
            raise ValueError("product event id does not match its product facts")
        return self


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
            return WorkflowProductEvent.model_validate_json(
                json.dumps(part.data, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError):
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
        event_fact = _event_fact(snapshot, event_type)
        return WorkflowProductEvent(
            event_id=_event_id_for_fact(event_fact),
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
        nodes = {}
        results = {}
    else:
        checked_plan = PersistedAdmittedWorkflowPlan.model_validate_json(
            json.dumps(plan)
        )
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
        if state["phase"] == "waiting_input"
        and state["status"] in {"waiting_input", "blocked"}
        else "waiting_input"
        if state["status"] == "waiting_input"
        else "failed"
        if state["status"] in {"failed", "cancelled", "blocked"}
        else "planning"
        if state["phase"] == "planning"
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
    # The error reducer is ACI and intentionally does not preserve causal order.
    # Product projection therefore uses stable generic terminal codes rather
    # than guessing that an arbitrary member caused the terminal state.
    if state["status"] == "failed":
        return "workflow_failed"
    if state["status"] == "cancelled":
        return "workflow_cancelled"
    return None


def _event_type(snapshot: WorkflowProductSnapshot) -> ProductEventType:
    if snapshot.handle.status == "completed":
        return "completed"
    if snapshot.handle.status == "cancelled":
        return "cancelled"
    if snapshot.handle.phase == "waiting_input" and snapshot.waiting_actions:
        return "waiting_input"
    if snapshot.handle.status in {"failed", "blocked"}:
        return "failed"
    if snapshot.handle.status == "waiting_input" or snapshot.waiting_actions:
        return "waiting_input"
    if snapshot.handle.phase == "planning":
        return "planning"
    if snapshot.handle.status == "queued":
        return "accepted"
    return "progress"


def _event_fact(
    snapshot: WorkflowProductSnapshot,
    event_type: ProductEventType,
) -> dict[str, object]:
    return {
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


def _event_id_for_fact(event_fact: Mapping[str, object]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            event_fact,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"workflow-event:sha256:{digest}"


__all__ = [
    "WorkflowActiveItem",
    "WorkflowGraphProjector",
    "WorkflowHandle",
    "WorkflowProductEvent",
    "WorkflowProductProgress",
    "WorkflowProductSnapshot",
    "WorkflowWaitingAction",
]
