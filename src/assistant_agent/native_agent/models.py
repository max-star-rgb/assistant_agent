"""Strict domain values used by the native planning graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkerResult(BaseModel):
    """One stable work-item result emitted by a parallel worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    work_item_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    revision: int = Field(default=0, ge=0, le=100)
    content: str = Field(min_length=1, max_length=100_000)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=128)


class PlanningArtifact(BaseModel):
    """Checkpoint-safe artifact value keyed by a stable business ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,159}$")
    content: str = Field(min_length=1, max_length=100_000)
    media_type: str = Field(default="text/plain", min_length=1, max_length=160)


class VerificationResult(BaseModel):
    """Structured verifier decision used by the bounded repair edge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed", "repair", "failed"]
    repair_work_item_ids: tuple[str, ...] = Field(default=(), max_length=64)
    reason: str = Field(default="", max_length=4_000)

    @field_validator("repair_work_item_ids", mode="before")
    @classmethod
    def _tuple_repair_ids(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_repair_targets(self) -> "VerificationResult":
        if self.status != "repair" and self.repair_work_item_ids:
            raise ValueError("only repair decisions may include work item IDs")
        if len(self.repair_work_item_ids) != len(set(self.repair_work_item_ids)):
            raise ValueError("repair work item IDs must be unique")
        return self


class PlanAcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    criterion_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    statement: str = Field(min_length=1, max_length=4_000)


class PlanArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_type: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    description: str = Field(min_length=1, max_length=4_000)


class PlanStepAcceptanceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["native_step_acceptance_v1"]
    output: PlanArtifactContract
    criteria: tuple[PlanAcceptanceCriterion, ...] = Field(min_length=1, max_length=64)

    @field_validator("criteria", mode="before")
    @classmethod
    def _tuple_criteria(cls, value):
        return tuple(value) if isinstance(value, list) else value


class NativePlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    display_title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)
    acceptance_contract: PlanStepAcceptanceContract

    @field_validator("depends_on", mode="before")
    @classmethod
    def _tuple_dependencies(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_dependencies(self) -> "NativePlanNode":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("node dependency ids must be unique")
        return self


class NativeDeliverableBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deliverable: str = Field(min_length=1, max_length=240)
    producer_node_id: str = Field(min_length=1, max_length=160)


class NativeConstraintBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    constraint_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    statement: str = Field(min_length=1, max_length=4_000)
    owner_node_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    verifier_node_id: str = Field(min_length=1, max_length=160)

    @field_validator("owner_node_ids", mode="before")
    @classmethod
    def _tuple_owner_ids(cls, value):
        return tuple(value) if isinstance(value, list) else value


class NativePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["native_plan_v1"]
    nodes: tuple[NativePlanNode, ...] = Field(min_length=1, max_length=128)
    deliverable_bindings: tuple[NativeDeliverableBinding, ...] = Field(
        min_length=1,
        max_length=32,
    )
    constraint_bindings: tuple[NativeConstraintBinding, ...] = Field(
        default=(),
        max_length=64,
    )

    @field_validator("nodes", "deliverable_bindings", "constraint_bindings", mode="before")
    @classmethod
    def _tuple_collections(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_local_ids(self) -> "NativePlanProposal":
        for values, label in (
            ([item.node_id for item in self.nodes], "node"),
            ([item.deliverable for item in self.deliverable_bindings], "deliverable"),
            ([item.constraint_id for item in self.constraint_bindings], "constraint"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"native plan {label} ids must be unique")
        return self


__all__ = [
    "NativeConstraintBinding",
    "NativeDeliverableBinding",
    "NativePlanNode",
    "NativePlanProposal",
    "PlanAcceptanceCriterion",
    "PlanArtifactContract",
    "PlanningArtifact",
    "PlanStepAcceptanceContract",
    "VerificationResult",
    "WorkerResult",
]
