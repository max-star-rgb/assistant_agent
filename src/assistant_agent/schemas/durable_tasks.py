"""Durable structured-task contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID
from assistant_agent.schemas.planning import TaskPlan


TaskStatus = Literal[
    "queued",
    "running",
    "waiting_schedule",
    "waiting_external_event",
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
    "waiting_schedule",
    "waiting_external_event",
    "waiting_input",
    "skipped",
    "cancelled",
    "outcome_unknown",
]
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskWaitState(BaseModel):
    """Structured, persisted reason why a durable task is not currently claimable."""

    kind: Literal["schedule", "external_event"]
    wait_id: str = Field(
        default_factory=lambda: f"task_wait_{uuid4().hex}",
        min_length=1,
    )
    reason_code: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    step_id: str | None = Field(default=None, min_length=1)
    next_eligible_at: datetime | None = None
    wake_rule_id: str | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_wait(self) -> "TaskWaitState":
        if self.kind == "schedule":
            if self.next_eligible_at is None:
                raise ValueError("scheduled wait requires next_eligible_at")
            if self.wake_rule_id is not None:
                raise ValueError("scheduled wait must not include wake_rule_id")
        else:
            if self.wake_rule_id is None:
                raise ValueError("external-event wait requires wake_rule_id")
            if self.next_eligible_at is not None:
                raise ValueError("external-event wait must not include next_eligible_at")
        if (
            self.next_eligible_at is not None
            and self.next_eligible_at.tzinfo is None
        ):
            raise ValueError("next_eligible_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must be timezone-aware")
            if (
                self.next_eligible_at is not None
                and self.expires_at <= self.next_eligible_at
            ):
                raise ValueError("expires_at must be later than next_eligible_at")
        return self


class TaskResumeRequest(BaseModel):
    """A ProactiveWake-produced request to resume one exact persisted wait."""

    task_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    expected_task_version: int = Field(ge=1)
    wait_id: str = Field(min_length=1)
    wake_rule_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    evidence_fingerprint: str = Field(min_length=1)
    requested_at: datetime = Field(default_factory=utc_now)


class TaskNotificationRequest(BaseModel):
    """A transport-neutral notification requested by one durable quantum."""

    channel: str = Field(default="mock_app", min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=240)
    evidence_ids: list[str] = Field(min_length=1)
    evidence_fingerprint: str = Field(min_length=1, max_length=240)
    deliver_after: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_delivery_window(self) -> "TaskNotificationRequest":
        if self.deliver_after.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("notification timestamps must be timezone-aware")
        if self.expires_at <= self.deliver_after:
            raise ValueError("notification expires_at must follow deliver_after")
        return self


class TaskRecord(BaseModel):
    task_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)
    session_id: str = Field(min_length=1)
    ingress_run_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    execution_profile: str = Field(default="agent", min_length=1)
    workflow_payload: dict[str, Any] = Field(default_factory=dict)
    workflow_state: dict[str, Any] = Field(default_factory=dict)
    active_constraints: list[str] = Field(default_factory=list)
    status: TaskStatus = "queued"
    current_plan_version: int = Field(default=1, ge=1)
    version: int = Field(default=1, ge=1)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    remaining_budget: dict[str, int | float] = Field(default_factory=dict)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    wait: TaskWaitState | None = None
    consumed_resume_keys: list[str] = Field(default_factory=list)
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
    created_at: datetime = Field(default_factory=utc_now)


class TaskStepRun(BaseModel):
    task_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    step_id: str = Field(min_length=1)
    status: TaskStepStatus = "pending"
    attempt: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1)
    tool_name: str | None = None
    side_effect_level: str = "none"
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
    execution_profile: str
    workflow_payload: dict[str, Any]
    workflow_state: dict[str, Any]
    active_constraints: list[str]
    task_status: TaskStatus
    plan_version: int
    plan: TaskPlan
    ready_step_ids: list[str]
    completed_steps: list[dict[str, str]]
    artifact_refs: list[TaskArtifactRef]
    wait: TaskWaitState | None = None
    remaining_budget: dict[str, int | float]


class TrustedTaskBinding(BaseModel):
    task_id: str
    task_version: int
    plan_version: int
    lease_owner: str
    lease_token: str
    ready_step_ids: list[str]
    step_idempotency_keys: dict[str, str] = Field(default_factory=dict)


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
        "waiting_schedule",
        "waiting_external_event",
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
    wait: TaskWaitState | None = None
    notification: TaskNotificationRequest | None = None
    workflow_state_patch: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_wait_checkpoint(self) -> "TaskCheckpoint":
        waiting_kind = {
            "waiting_schedule": "schedule",
            "waiting_external_event": "external_event",
        }.get(self.kind)
        if waiting_kind is not None and self.wait is None:
            raise ValueError(f"{self.kind} checkpoint requires wait")
        if waiting_kind is None and self.wait is not None:
            raise ValueError("wait is only valid for waiting checkpoints")
        if self.wait is not None and self.wait.kind != waiting_kind:
            raise ValueError("checkpoint kind does not match wait kind")
        if self.notification is not None and self.kind != "completed":
            raise ValueError("notification is only valid for completed checkpoint")
        return self
