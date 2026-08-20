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
    """A required final response item produced by a node or planner evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deliverable_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    description: str = Field(min_length=1, max_length=2_000)
    producer_node_ids: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("producer_node_ids", "evidence_refs", mode="before")
    @classmethod
    def _tuple_collections(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _has_producer(self) -> "PlanDeliverable":
        if not self.producer_node_ids and not self.evidence_refs:
            raise ValueError("deliverable requires a node producer or evidence")
        return self


class NativePlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)
    required_skill_ids: tuple[str, ...] = Field(default=(), max_length=16)
    allowed_tool_names: tuple[str, ...] = Field(default=(), max_length=64)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    search_profile: ProviderSearchProfile = "none"

    @field_validator(
        "depends_on",
        "required_skill_ids",
        "allowed_tool_names",
        "evidence_refs",
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
        return self


class NativePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["native_plan_v1"]
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


__all__ = [
    "EvidenceLink",
    "NativePlanNode",
    "NativePlanProposal",
    "PlanDeliverable",
    "PlannerEvidence",
    "ProviderSearchProfile",
    "WorkerResult",
]
