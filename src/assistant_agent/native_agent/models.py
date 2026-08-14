"""Strict domain values used by the native planning graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkerResult(BaseModel):
    """One work-item result emitted by a planning worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    work_item_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    content: str = Field(min_length=1, max_length=100_000)


class NativePlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _tuple_dependencies(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_dependencies(self) -> "NativePlanNode":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("node dependency ids must be unique")
        return self


class NativePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["native_plan_v1"]
    nodes: tuple[NativePlanNode, ...] = Field(min_length=1, max_length=128)

    @field_validator("nodes", mode="before")
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
    "NativePlanNode",
    "NativePlanProposal",
    "WorkerResult",
]
