"""Typed contracts for governed local memory intelligence."""

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator


MemoryFactStatus = Literal["active", "superseded", "disputed", "retracted"]
MemoryFactProvenance = Literal[
    "user_explicit",
    "user_confirmed",
    "tool_verified",
    "assistant_inferred",
    "imported",
]
MemoryConflictPolicy = Literal["replace", "coexist", "confirm"]
MemoryConflictAction = Literal["append", "merge", "supersede", "coexist", "confirm"]


class MemoryFact(BaseModel):
    """A versioned fact envelope stored inside ``MemoryItem.content``."""

    schema_version: Literal[1] = 1
    fact_key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)
    status: MemoryFactStatus = "active"
    provenance: MemoryFactProvenance
    conflict_policy: MemoryConflictPolicy = "confirm"
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    revision: int = Field(default=1, ge=1)
    supersedes_memory_ids: list[str] = Field(default_factory=list)
    superseded_by_memory_id: str | None = None
    conflict_reason: str | None = None

    @model_validator(mode="after")
    def validate_fact_state(self) -> "MemoryFact":
        self.fact_key = normalize_fact_key(self.fact_key)
        self.subject = self.subject.strip().lower()
        self.predicate = self.predicate.strip().lower()
        self.value = self.value.strip()
        self.supersedes_memory_ids = list(
            dict.fromkeys(value.strip() for value in self.supersedes_memory_ids if value.strip())
        )
        if self.superseded_by_memory_id is not None:
            self.superseded_by_memory_id = self.superseded_by_memory_id.strip() or None
        if self.conflict_reason is not None:
            self.conflict_reason = self.conflict_reason.strip() or None
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.status == "active" and self.superseded_by_memory_id is not None:
            raise ValueError("active fact cannot be superseded")
        return self


class MemoryConflictDecision(BaseModel):
    """Pure conflict decision returned before any store mutation."""

    action: MemoryConflictAction
    reason: str = Field(min_length=1)
    fact_key: str | None = None
    matching_memory_ids: list[str] = Field(default_factory=list)
    superseded_memory_ids: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False

    @model_validator(mode="after")
    def normalize_decision(self) -> "MemoryConflictDecision":
        if self.fact_key is not None:
            self.fact_key = normalize_fact_key(self.fact_key)
        self.matching_memory_ids = sorted(set(self.matching_memory_ids))
        self.superseded_memory_ids = sorted(set(self.superseded_memory_ids))
        return self


def normalize_fact_key(value: str) -> str:
    """Normalize an opaque fact slot key without semantic inference."""

    parts = [part for part in re.split(r"[^a-z0-9\u4e00-\u9fff]+", value.strip().lower()) if part]
    if not parts:
        raise ValueError("fact identifier must contain alphanumeric or CJK characters")
    return ":".join(parts)
