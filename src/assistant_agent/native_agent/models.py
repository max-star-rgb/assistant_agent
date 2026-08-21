"""Strict domain values used by the native planning graph."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


ProviderSearchProfile = Literal[
    "none",
    "rail_official",
    "flight_official",
    "guide_official",
    "guide_xiaohongshu",
    "travel_general",
]


class BudgetUsage(BaseModel):
    """Counters consumed by one phase or one recovery transition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    node_attempts: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)


FailureCategory = Literal[
    "budget_exhausted",
    "operational",
    "business_failure",
    "authorization",
    "contract_bug",
]


class FailureFact(BaseModel):
    """A stable, local classification of an execution failure."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: FailureCategory
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    phase: Literal["planner", "worker", "finalizer"]
    plan_generation: int = Field(ge=0)
    work_item_id: str | None = Field(default=None, max_length=120)
    attempt: int = Field(ge=1)


class EvidenceLink(BaseModel):
    """A sanitized https citation produced by the provider, not by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    index: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    domain: str = Field(min_length=1, max_length=253)


class WorkerResult(BaseModel):
    """One work-item result emitted by a planning worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    work_item_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    content: str = Field(min_length=1, max_length=100_000)
    verification_status: Literal["verified", "advisory", "unverified", "failed"] = (
        "advisory"
    )
    sources: tuple[EvidenceLink, ...] = Field(default=(), max_length=20)

    @field_validator("sources", mode="before")
    @classmethod
    def _tuple_sources(cls, value):
        return tuple(value) if isinstance(value, list) else value


class PlannerEvidence(BaseModel):
    """Bounded evidence captured from a planner Tool invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,159}$")
    tool_name: str = Field(min_length=1, max_length=160)
    status: Literal["succeeded", "failed"]
    content: str = Field(min_length=1, max_length=20_000)
    structured_content: JsonValue | None = None
    artifact_ref: str | None = Field(default=None, max_length=2_000)


class PlanDeliverable(BaseModel):
    """A required final response item from current, evidence, or frozen output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deliverable_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    description: str = Field(min_length=1, max_length=2_000)
    producer_node_ids: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    frozen_result_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator(
        "producer_node_ids", "evidence_refs", "frozen_result_refs", mode="before"
    )
    @classmethod
    def _tuple_collections(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _has_producer(self) -> "PlanDeliverable":
        if not (
            self.producer_node_ids or self.evidence_refs or self.frozen_result_refs
        ):
            raise ValueError(
                "deliverable requires a node producer, evidence, or frozen result"
            )
        return self


class NativePlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)
    required_skill_ids: tuple[str, ...] = Field(default=(), max_length=16)
    allowed_tool_names: tuple[str, ...] = Field(default=(), max_length=64)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    replaces_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    frozen_dependency_ids: tuple[str, ...] = Field(default=(), max_length=64)
    search_profile: ProviderSearchProfile = "none"

    @field_validator(
        "depends_on",
        "required_skill_ids",
        "allowed_tool_names",
        "evidence_refs",
        "replaces_node_ids",
        "frozen_dependency_ids",
        mode="before",
    )
    @classmethod
    def _tuple_collections(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_dependencies(self) -> "NativePlanNode":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("node dependency ids must be unique")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("node evidence refs must be unique")
        if len(self.replaces_node_ids) != len(set(self.replaces_node_ids)):
            raise ValueError("node replacement ids must be unique")
        if len(self.frozen_dependency_ids) != len(set(self.frozen_dependency_ids)):
            raise ValueError("node frozen dependency ids must be unique")
        return self


class NativePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["native_plan_v2"]
    nodes: tuple[NativePlanNode, ...] = Field(max_length=128)
    deliverables: tuple[PlanDeliverable, ...] = Field(min_length=1, max_length=64)

    @field_validator("nodes", "deliverables", mode="before")
    @classmethod
    def _tuple_collections(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_local_ids(self) -> "NativePlanProposal":
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("native plan node ids must be unique")
        return self


class PlannerOutcome(BaseModel):
    """Structured result of one planner attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["succeeded", "budget_exhausted", "operational_failed"]
    plan_candidate: NativePlanProposal | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=128)
    failure: FailureFact | None = None
    usage: BudgetUsage

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _tuple_evidence_ids(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_status_payload(self) -> "PlannerOutcome":
        if self.status == "succeeded":
            if self.plan_candidate is None:
                raise ValueError("successful planner outcome requires plan_candidate")
            if self.failure is not None:
                raise ValueError("successful planner outcome cannot have failure")
        else:
            if self.plan_candidate is not None:
                raise ValueError("failed planner outcome cannot have plan_candidate")
            if self.failure is None:
                raise ValueError("failed planner outcome requires failure")
            expected_category = {
                "budget_exhausted": "budget_exhausted",
                "operational_failed": "operational",
            }[self.status]
            if self.failure.phase != "planner":
                raise ValueError("planner failure must have planner phase")
            if self.failure.category != expected_category:
                raise ValueError("planner failure category does not match status")
            if self.failure.work_item_id is not None:
                raise ValueError("planner failure cannot have work_item_id")
        return self


class WorkerCompletion(BaseModel):
    """Strict structured response produced by a planning worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["completed", "insufficient"]
    content: str = Field(min_length=1, max_length=100_000)


class WorkerOutcome(BaseModel):
    """Structured result of one deterministic worker execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: str = Field(min_length=1, max_length=240)
    plan_generation: int = Field(ge=0)
    work_item_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    attempt: int = Field(ge=1)
    status: Literal[
        "succeeded",
        "budget_exhausted",
        "operational_failed",
        "business_failed",
    ]
    result: WorkerResult | None = None
    failure: FailureFact | None = None
    usage: BudgetUsage

    @model_validator(mode="after")
    def _validate_status_payload(self) -> "WorkerOutcome":
        canonical_execution_id = (
            f"g{self.plan_generation}:{self.work_item_id}:a{self.attempt}"
        )
        if self.execution_id != canonical_execution_id:
            raise ValueError("worker outcome requires canonical execution_id")
        if self.status == "succeeded":
            if self.result is None:
                raise ValueError("successful worker outcome requires result")
            if self.failure is not None:
                raise ValueError("successful worker outcome cannot have failure")
            if self.result.work_item_id != self.work_item_id:
                raise ValueError("worker result work_item_id does not match outcome")
        else:
            if self.result is not None:
                raise ValueError("failed worker outcome cannot have result")
            if self.failure is None:
                raise ValueError("failed worker outcome requires failure")
            expected_category = {
                "budget_exhausted": "budget_exhausted",
                "operational_failed": "operational",
                "business_failed": "business_failure",
            }[self.status]
            if self.failure.phase != "worker":
                raise ValueError("worker failure must have worker phase")
            if self.failure.category != expected_category:
                raise ValueError("worker failure category does not match status")
            if self.failure.plan_generation != self.plan_generation:
                raise ValueError(
                    "worker failure plan_generation does not match outcome"
                )
            if self.failure.work_item_id != self.work_item_id:
                raise ValueError("worker failure work_item_id does not match outcome")
            if self.failure.attempt != self.attempt:
                raise ValueError("worker failure attempt does not match outcome")
        return self


class RecoveryDecision(BaseModel):
    """Deterministic action selected by the local recovery router."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["retry", "replan", "finalize", "propagate"]
    reason_code: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,119}$",
        min_length=1,
    )
    source_execution_ids: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("source_execution_ids", mode="before")
    @classmethod
    def _tuple_execution_ids(cls, value):
        return tuple(value) if isinstance(value, list) else value


__all__ = [
    "BudgetUsage",
    "EvidenceLink",
    "FailureCategory",
    "FailureFact",
    "NativePlanNode",
    "NativePlanProposal",
    "PlanDeliverable",
    "PlannerEvidence",
    "PlannerOutcome",
    "ProviderSearchProfile",
    "RecoveryDecision",
    "WorkerCompletion",
    "WorkerOutcome",
    "WorkerResult",
]
