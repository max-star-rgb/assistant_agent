"""Durable structured-task contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.planning import TaskPlan


TaskStatus = Literal[
    "queued",
    "running",
    "waiting_confirmation",
    "waiting_input",
    "replanning",
    "outcome_unknown",
    "completed",
    "failed",
    "cancelled",
]
TaskStepStatus = Literal[
    "pending",
    "ready",
    "leased",
    "running",
    "succeeded",
    "failed",
    "waiting_confirmation",
    "waiting_input",
    "skipped",
    "cancelled",
    "outcome_unknown",
]
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRecord(BaseModel):
    task_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    ingress_run_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    active_constraints: list[str] = Field(default_factory=list)
    status: TaskStatus = "queued"
    current_plan_version: int = Field(default=1, ge=1)
    version: int = Field(default=1, ge=1)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    remaining_budget: dict[str, int | float] = Field(default_factory=dict)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    terminal_at: datetime | None = None


class TaskPlanVersion(BaseModel):
    task_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    plan: TaskPlan
    revision_reason: str = Field(min_length=1)
    inherited_step_ids: list[str] = Field(default_factory=list)
    replaced_step_ids: list[str] = Field(default_factory=list)
    invalidated_confirmation_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class TaskStepRun(BaseModel):
    task_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    step_id: str = Field(min_length=1)
    status: TaskStepStatus = "pending"
    attempt: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1)
    tool_name: str | None = None
    tool_input_digest: str | None = None
    output_ref: str | None = None
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskEvent(BaseModel):
    task_id: str = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    event_type: str = Field(min_length=1)
    status: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TaskConfirmation(BaseModel):
    confirmation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    step_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    input_digest: str = Field(min_length=1)
    binding_digest: str = Field(min_length=1)
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    expires_at: datetime
    decided_by_user_id: str | None = None
    decided_at: datetime | None = None


class TaskArtifactRef(BaseModel):
    artifact_ref: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    summary: str = ""
    producer_plan_version: int = Field(ge=1)
    producer_step_id: str = Field(min_length=1)
    trust: str = "tool_result"


class DurableTaskBundle(BaseModel):
    task: TaskRecord
    plans: list[TaskPlanVersion] = Field(min_length=1)
    step_runs: list[TaskStepRun] = Field(default_factory=list)
    confirmations: list[TaskConfirmation] = Field(default_factory=list)
    artifacts: list[TaskArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "DurableTaskBundle":
        versions = [plan.plan_version for plan in self.plans]
        if len(versions) != len(set(versions)):
            raise ValueError("plan versions must be unique")
        if self.task.current_plan_version not in versions:
            raise ValueError("current_plan_version must reference an existing plan")
        if any(plan.task_id != self.task.task_id for plan in self.plans):
            raise ValueError("plans must reference the bundle task")
        plans_by_version = {plan.plan_version: plan for plan in self.plans}
        for run in self.step_runs:
            plan_version = plans_by_version.get(run.plan_version)
            if plan_version is None or run.task_id != self.task.task_id:
                raise ValueError("step run must reference an existing task plan")
            if run.step_id not in {step.step_id for step in plan_version.plan.steps}:
                raise ValueError("step run must reference an existing plan step")
        if self.task.status in TERMINAL_TASK_STATUSES and self.task.terminal_at is None:
            raise ValueError("terminal_at is required for terminal task status")
        return self


class DurableTaskSnapshot(BaseModel):
    task_id: str
    objective: str
    active_constraints: list[str]
    task_status: TaskStatus
    plan_version: int
    plan: TaskPlan
    ready_step_ids: list[str]
    completed_steps: list[dict[str, str]]
    artifact_refs: list[TaskArtifactRef]
    wait: dict[str, Any] | None = None
    remaining_budget: dict[str, int | float]


class TrustedTaskBinding(BaseModel):
    task_id: str
    task_version: int
    plan_version: int
    lease_owner: str
    lease_token: str
    ready_step_ids: list[str]
    step_idempotency_keys: dict[str, str] = Field(default_factory=dict)
    verified_confirmation_id: str | None = None
    verified_confirmation_tool_name: str | None = None
    verified_confirmation_input_digest: str | None = None


class DurableTaskLease(BaseModel):
    task_id: str
    task_version: int
    worker_id: str
    lease_token: str
    expires_at: datetime


class TaskCheckpoint(BaseModel):
    kind: Literal[
        "tool_succeeded",
        "tool_failed",
        "waiting_confirmation",
        "waiting_input",
        "plan_revised",
        "completed",
        "failed",
        "cancelled",
        "outcome_unknown",
    ]
    step_id: str | None = None
    output_ref: str | None = None
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
    tool_name: str | None = None
    tool_input_digest: str | None = None
    confirmation_expires_at: datetime | None = None
