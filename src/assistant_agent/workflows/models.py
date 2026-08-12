"""Provider-neutral durable workflow contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator



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
ConstraintSeverity = Literal["required", "advisory"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls: int | None = Field(default=None, ge=1, le=10_000)
    tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    workflow_quanta: int | None = Field(default=None, ge=1, le=1_000_000)
    deadline_seconds: int | None = Field(default=None, ge=60, le=2_592_000)


class WorkflowConstraintProposal(BaseModel):
    """Planner proposal whose verifier may depend on the not-yet-built DAG."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    statement: str = Field(min_length=1, max_length=4_000)
    owner_work_item_ids: list[str] = Field(min_length=1, max_length=64)
    verifier_work_item_id: str | None = Field(default=None, min_length=1, max_length=160)
    severity: ConstraintSeverity = "required"

    @model_validator(mode="after")
    def validate_owners(self) -> "WorkflowConstraintProposal":
        if len(self.owner_work_item_ids) != len(set(self.owner_work_item_ids)):
            raise ValueError("constraint owner work item ids must be unique")
        return self


class WorkflowConstraintBinding(WorkflowConstraintProposal):
    """Admitted Plan binding with complete verification routing."""

    @model_validator(mode="after")
    def validate_required_verifier(self) -> "WorkflowConstraintBinding":
        if self.severity == "required" and self.verifier_work_item_id is None:
            raise ValueError("required constraint must declare a verifier")
        return self


class WorkflowSeedWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=120)
    display_title: str = Field(min_length=1, max_length=160)
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
    requested_budget: WorkflowBudgetRequest = Field(
        default_factory=WorkflowBudgetRequest
    )
    durability_reasons: list[str] = Field(min_length=1, max_length=16)
    seed_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=240)


class WorkflowPlanProposal(BaseModel):
    """Legacy v1 DAG produced by the durable Workflow planner Agent."""

    model_config = ConfigDict(extra="forbid")

    workstreams: list[WorkflowSeedWorkItem] = Field(min_length=1, max_length=128)
    constraint_bindings: list[WorkflowConstraintProposal] = Field(
        default_factory=list,
        max_length=64,
    )


class WorkflowAcceptanceCriterion(BaseModel):
    """One locally owned and independently identifiable completion criterion."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    statement: str = Field(min_length=1, max_length=4_000)


class WorkflowArtifactContract(BaseModel):
    """The primary artifact a node must make available to its descendants."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    description: str = Field(min_length=1, max_length=4_000)


class WorkflowStepAcceptanceContract(BaseModel):
    """Typed, node-local completion contract for a v2 plan node."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workflow_step_acceptance_v2"]
    output: WorkflowArtifactContract
    criteria: list[WorkflowAcceptanceCriterion] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_criterion_ids(self) -> "WorkflowStepAcceptanceContract":
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion ids must be unique within a node")
        return self


class WorkflowPlanNodeV2(BaseModel):
    """One generic Agent work item in a static v2 DAG."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    display_title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    acceptance_contract: WorkflowStepAcceptanceContract

    @model_validator(mode="after")
    def validate_dependencies(self) -> "WorkflowPlanNodeV2":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("node dependency ids must be unique")
        return self


class WorkflowDeliverableBindingProposal(BaseModel):
    """Planner-declared ownership of one requested Workflow deliverable."""

    model_config = ConfigDict(extra="forbid")

    deliverable: str = Field(min_length=1, max_length=240)
    producer_node_id: str = Field(min_length=1, max_length=160)


class WorkflowDeliverableBinding(BaseModel):
    """Admitted deliverable ownership using persisted work-item identity."""

    model_config = ConfigDict(extra="forbid")

    deliverable: str = Field(min_length=1, max_length=240)
    producer_work_item_id: str = Field(min_length=1, max_length=160)


class WorkflowConstraintProposalV2(BaseModel):
    """v2 constraint ownership expressed in node terminology."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    statement: str = Field(min_length=1, max_length=4_000)
    owner_node_ids: list[str] = Field(min_length=1, max_length=64)
    verifier_node_id: str | None = Field(default=None, min_length=1, max_length=160)
    severity: ConstraintSeverity = "required"

    @model_validator(mode="after")
    def validate_owners(self) -> "WorkflowConstraintProposalV2":
        if len(self.owner_node_ids) != len(set(self.owner_node_ids)):
            raise ValueError("constraint owner node ids must be unique")
        if self.severity == "required" and self.verifier_node_id is None:
            raise ValueError("required constraint must declare a verifier node")
        return self


class WorkflowPlanV2Proposal(BaseModel):
    """Generic, typed and explicitly versioned static DAG proposal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workflow_plan_v2"]
    nodes: list[WorkflowPlanNodeV2] = Field(min_length=1, max_length=128)
    deliverable_bindings: list[WorkflowDeliverableBindingProposal] = Field(
        min_length=1,
        max_length=32,
    )
    constraint_bindings: list[WorkflowConstraintProposalV2] = Field(
        default_factory=list,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_local_ids(self) -> "WorkflowPlanV2Proposal":
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow plan node ids must be unique")
        deliverables = [item.deliverable for item in self.deliverable_bindings]
        if len(deliverables) != len(set(deliverables)):
            raise ValueError("workflow deliverable bindings must be unique")
        constraint_ids = [item.constraint_id for item in self.constraint_bindings]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("workflow constraint ids must be unique")
        return self


WorkflowPlannerProposal = WorkflowPlanProposal | WorkflowPlanV2Proposal


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
    display_title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=10_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    acceptance_contract: WorkflowStepAcceptanceContract | dict[str, JsonValue] = Field(
        default_factory=dict
    )
    status: WorkItemStatus = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    active_attempt_id: str | None = Field(default=None, min_length=1)
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_token: str | None = Field(default=None, min_length=1)
    lease_expires_at: datetime | None = None
    reserved_model_calls: int = Field(default=0, ge=0)
    reserved_tool_calls: int = Field(default=0, ge=0)
    output_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    result_summary: str = Field(default="", max_length=4_000)
    error_code: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_attempt_lease(self) -> "WorkflowWorkItem":
        lease_values = (
            self.active_attempt_id,
            self.lease_owner,
            self.lease_token,
            self.lease_expires_at,
        )
        if any(value is None for value in lease_values) and any(
            value is not None for value in lease_values
        ):
            raise ValueError("attempt id, lease owner, token, and expiry must be set together")
        if self.lease_expires_at is not None and self.lease_expires_at.tzinfo is None:
            raise ValueError("work item lease expiry must be timezone-aware")
        if self.status == "running" and self.lease_token is None:
            raise ValueError("running work item requires an active lease")
        if self.lease_token is None and (
            self.reserved_model_calls or self.reserved_tool_calls
        ):
            raise ValueError("budget reservations require an active lease")
        return self


class WorkflowPlanVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    definition_version: str = Field(min_length=1, max_length=80)
    revision_reason: str = Field(min_length=1, max_length=500)
    work_items: list[WorkflowWorkItem] = Field(min_length=1, max_length=256)
    constraint_bindings: list[WorkflowConstraintBinding] = Field(
        default_factory=list,
        max_length=64,
    )
    deliverable_bindings: list[WorkflowDeliverableBinding] = Field(
        default_factory=list,
        max_length=32,
    )
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
    ingress_trace_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
    )
    ingress_parent_span_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
    )
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_workflow_lease(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        for name in ("lease_owner", "lease_token", "lease_expires_at"):
            migrated.pop(name, None)
        return migrated

    @model_validator(mode="after")
    def validate_times(self) -> "WorkflowRecord":
        for name in ("created_at", "updated_at", "terminal_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.ingress_parent_span_id is not None and self.ingress_trace_id is None:
            raise ValueError(
                "ingress parent span id requires an ingress trace id"
            )
        return self


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    event_type: str = Field(min_length=1, max_length=160)
    status: str = Field(min_length=1, max_length=80)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowWorkItemLease(BaseModel):
    """Durable ownership of one independently executable DAG node."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    workflow_revision: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    expires_at: datetime
    reserved_model_calls: int = Field(ge=1)
    reserved_tool_calls: int = Field(ge=0)


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


class WorkflowDispatch(BaseModel):
    """One committed scheduler update, optionally carrying executable ownership."""

    model_config = ConfigDict(extra="forbid")

    lease: WorkflowWorkItemLease | None = None
    bundle: WorkflowBundle
    committed_events: list[WorkflowEvent] = Field(default_factory=list)
