"""Schemas for memory audit APIs."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.schemas.api import PROTOCOL_VERSION
from multimodal_agent.schemas.memory import MemoryItem, MemoryType


class MemoryAuditItem(BaseModel):
    """Public, user-scoped memory item view."""

    memory_id: str
    user_id: str
    session_id: str | None = None
    memory_type: MemoryType
    summary: str
    tags: list[str] = Field(default_factory=list)
    source: str
    artifact_refs: list[str] = Field(default_factory=list)
    relevance: float | None = None
    reason: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    sensitivity: str
    content: dict[str, Any] | None = None

    @classmethod
    def from_memory(cls, item: MemoryItem, *, include_content: bool = False) -> "MemoryAuditItem":
        return cls(
            memory_id=item.memory_id,
            user_id=item.user_id,
            session_id=item.session_id,
            memory_type=item.memory_type,
            summary=item.summary,
            tags=item.tags,
            source=item.source,
            artifact_refs=item.artifact_refs,
            relevance=item.relevance,
            reason=item.reason,
            created_at=item.created_at,
            updated_at=item.updated_at,
            expires_at=item.expires_at,
            sensitivity=item.sensitivity,
            content=item.content if include_content else None,
        )


class MemoryAuditList(BaseModel):
    """List response for memory audit."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    total: int = Field(ge=0)
    items: list[MemoryAuditItem] = Field(default_factory=list)


class MemoryDuplicateGroup(BaseModel):
    """Potential duplicate memory group."""

    key: str
    memory_type: MemoryType
    memory_ids: list[str] = Field(default_factory=list)
    summary: str


class MemoryAuditReport(BaseModel):
    """Audit report for one user's memory space."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    total: int = Field(ge=0)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    sensitive_count: int = Field(ge=0)
    expired_count: int = Field(ge=0)
    duplicate_groups: list[MemoryDuplicateGroup] = Field(default_factory=list)
    profile_present: bool = False
    profile_memory_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MemoryDeleteResult(BaseModel):
    """Delete response for memory audit APIs."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    deleted: dict[str, int] = Field(default_factory=dict)
