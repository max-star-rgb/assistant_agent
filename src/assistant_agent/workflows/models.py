"""Provider-neutral durable workflow contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from assistant_agent.planning_contracts import PlanDisplayTitle


WorkflowStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "blocked",
    "recovering",
    "completed",
    "failed",
    "cancelled",
]
WorkItemStatus = Literal[
    "pending",
    "ready",
    "running",
    "succeeded",
    "retryable_failed",
    "blocked",
    "superseded",
    "skipped",
    "cancelled",
]
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls: int | None = Field(default=None, ge=1, le=10_000)
    tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    workflow_quanta: int | None = Field(default=None, ge=1, le=1_000_000)
    deadline_seconds: int | None = Field(default=None, ge=60, le=2_592_000)


class WorkflowSeedWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=120)
    display_title: PlanDisplayTitle = None
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    acceptance_contract: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: list[str] = Field(min_length=1, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    initial_workstreams: list[WorkflowSeedWorkItem] = Field(
        default_factory=list,
        max_length=128,
        description=(
            "Agent 为该长期任务预先生成的可执行 DAG。任务可合理拆解时应提供；"
            "每项同时声明依赖、内部 objective 和用户可见 display_title。"
        ),
    )
    requested_budget: WorkflowBudgetRequest = Field(
        default_factory=WorkflowBudgetRequest
    )
    durability_reasons: list[str] = Field(min_length=1, max_length=16)
    seed_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=240)


class WorkflowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    workflow_quanta_remaining: int = Field(ge=0)
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_deadline(self) -> "WorkflowBudget":
        if self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at must be timezone-aware")
        return self


class WorkflowWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=120)
    display_title: PlanDisplayTitle = None
    objective: str = Field(min_length=1, max_length=10_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    acceptance_contract: dict[str, JsonValue] = Field(default_factory=dict)
    status: WorkItemStatus = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    active_attempt_id: str | None = Field(default=None, min_length=1)
    output_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    result_summary: str = Field(default="", max_length=4_000)
    error_code: str | None = Field(default=None, max_length=160)


class WorkflowPlanVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    definition_version: str = Field(min_length=1, max_length=80)
    revision_reason: str = Field(min_length=1, max_length=500)
    work_items: list[WorkflowWorkItem] = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    workflow_type: str = Field(min_length=1, max_length=80)
    definition_version: str = Field(min_length=1, max_length=80)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    ingress_run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=240)
    submission_digest: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: list[str] = Field(min_length=1, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    status: WorkflowStatus = "queued"
    phase: str = Field(default="admitted", min_length=1, max_length=120)
    current_plan_version: int = Field(default=1, ge=1)
    revision: int = Field(default=1, ge=1)
    cancel_requested: bool = False
    budget: WorkflowBudget
    seed_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    terminal_reason_code: str | None = Field(default=None, max_length=160)
    result_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    waiting_input: dict[str, JsonValue] | None = None
    consumed_resume_tokens: list[str] = Field(default_factory=list, max_length=1_000)
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_token: str | None = Field(default=None, min_length=1)
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def validate_times_and_lease(self) -> "WorkflowRecord":
        for name in ("created_at", "updated_at", "lease_expires_at", "terminal_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        lease_values = (self.lease_owner, self.lease_token, self.lease_expires_at)
        if any(value is None for value in lease_values) and any(
            value is not None for value in lease_values
        ):
            raise ValueError("lease owner, token, and expiry must be set together")
        return self


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    event_type: str = Field(min_length=1, max_length=160)
    status: str = Field(min_length=1, max_length=80)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    workflow_revision: int = Field(ge=1)
    worker_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    expires_at: datetime


class WorkflowBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowRecord
    plans: list[WorkflowPlanVersion] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "WorkflowBundle":
        versions = [plan.version for plan in self.plans]
        if len(versions) != len(set(versions)):
            raise ValueError("plan versions must be unique")
        if self.workflow.current_plan_version not in versions:
            raise ValueError("current_plan_version must reference an existing plan")
        for plan in self.plans:
            if plan.workflow_id != self.workflow.workflow_id:
                raise ValueError("plans must reference the bundle workflow")
            ids = [item.work_item_id for item in plan.work_items]
            if len(ids) != len(set(ids)):
                raise ValueError("work item ids must be unique within a plan")
        terminal = self.workflow.status in TERMINAL_WORKFLOW_STATUSES
        if terminal and self.workflow.terminal_at is None:
            raise ValueError("terminal_at is required for terminal workflow status")
        if not terminal and self.workflow.terminal_at is not None:
            raise ValueError("terminal_at is forbidden for non-terminal workflow status")
        return self

    @property
    def current_plan(self) -> WorkflowPlanVersion:
        return next(
            plan
            for plan in self.plans
            if plan.version == self.workflow.current_plan_version
        )
