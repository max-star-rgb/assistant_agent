"""Strict domain values used by the native planning graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkerResult(BaseModel):
    """One stable work-item result emitted by a parallel worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    work_item_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
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

    @model_validator(mode="after")
    def _validate_repair_targets(self) -> "VerificationResult":
        if self.status != "repair" and self.repair_work_item_ids:
            raise ValueError("only repair decisions may include work item IDs")
        if len(self.repair_work_item_ids) != len(set(self.repair_work_item_ids)):
            raise ValueError("repair work item IDs must be unique")
        return self


__all__ = ["PlanningArtifact", "VerificationResult", "WorkerResult"]
