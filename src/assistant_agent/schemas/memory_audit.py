"""Schemas for memory audit APIs."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.api import PROTOCOL_VERSION
from assistant_agent.schemas.memory import MemoryItem, MemoryType
from assistant_agent.schemas.memory_core import MemoryCoreStatus

MemoryAuditEventType = Literal[
    "memory_context_loaded",
    "memory_explicit_saved",
    "memory_updated",
    "memory_confirmation_created",
    "memory_confirmation_decided",
    "memory_promotion_decided",
    "memory_deleted",
    "memory_hard_deleted",
    "memory_session_deleted",
    "memory_user_cleared",
    "memory_exported",
    "memory_retention_swept",
    "memory_profile_repaired",
    "memory_remote_degraded",
    "memory_remote_lifecycle_failed",
    "memory_framework_degraded",
]
MemoryAuditEventOutcome = Literal["succeeded", "skipped", "rejected", "failed"]
MemoryProfileRepairAction = Literal["none", "create", "update", "delete"]
MemoryConfirmationStatus = Literal["pending", "confirmed", "rejected", "expired"]
MemoryConfirmationKind = Literal["write_policy", "fact_conflict"]


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


class MemoryUpdateRequest(BaseModel):
    """User-authored replacement text for one durable memory."""

    text: str = Field(min_length=1, max_length=4000)


class MemoryExport(BaseModel):
    """User-scoped memory export."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    exported_at: datetime
    include_content: bool
    total: int = Field(ge=0)
    items: list[MemoryAuditItem] = Field(default_factory=list)


class MemoryRetentionSweepResult(BaseModel):
    """Result of an expired-memory retention sweep."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    mode: Literal["soft_delete", "hard_delete"]
    dry_run: bool = False
    scanned: int = Field(ge=0)
    expired: int = Field(ge=0)
    deleted: dict[str, int] = Field(default_factory=dict)
    memory_ids: list[str] = Field(default_factory=list)


class MemoryAuditEvent(BaseModel):
    """Prompt-safe lifecycle event emitted by the memory service boundary."""

    event_id: str
    event_type: MemoryAuditEventType
    user_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    memory_id: str | None = None
    occurred_at: datetime
    outcome: MemoryAuditEventOutcome = "succeeded"
    summary: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryAuditEventList(BaseModel):
    """User-scoped list of memory audit events."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    total: int = Field(ge=0)
    items: list[MemoryAuditEvent] = Field(default_factory=list)


class MemoryMetricsReport(BaseModel):
    """Aggregated local counters derived from memory audit events."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    core_status: MemoryCoreStatus | None = None
    total_events: int = Field(ge=0)
    by_event_type: dict[str, int] = Field(default_factory=dict)
    by_outcome: dict[str, int] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)


class MemoryPendingConfirmation(BaseModel):
    """Prompt-safe pending explicit-memory confirmation."""

    confirmation_id: str
    user_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    memory_id: str | None = None
    confirmation_kind: MemoryConfirmationKind = "write_policy"
    fact_key: str | None = None
    conflict_memory_ids: list[str] = Field(default_factory=list)
    status: MemoryConfirmationStatus = "pending"
    memory_type: MemoryType
    scope: str | None = None
    destination: str
    sensitivity: str
    reason: str
    summary: str
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    content_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    confirmed_memory_id: str | None = None


class MemoryConfirmationList(BaseModel):
    """User-scoped list of pending or resolved memory confirmations."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    total: int = Field(ge=0)
    items: list[MemoryPendingConfirmation] = Field(default_factory=list)


class MemoryConfirmationResult(BaseModel):
    """Result of a memory confirmation decision."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    confirmation_id: str
    status: MemoryConfirmationStatus
    memory_id: str | None = None
    confirmation: MemoryPendingConfirmation


class MemoryProfileRepairResult(BaseModel):
    """User-profile rebuild/repair status."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    dry_run: bool = False
    repaired: bool = False
    action: MemoryProfileRepairAction = "none"
    profile_memory_id: str | None = None
    profile_present_before: bool = False
    profile_present_after: bool = False
    source_count: int = Field(ge=0)
    expected_source_memory_ids: list[str] = Field(default_factory=list)
    current_source_memory_ids: list[str] = Field(default_factory=list)
    missing_source_memory_ids: list[str] = Field(default_factory=list)
    stale_source_memory_ids: list[str] = Field(default_factory=list)
    superseded_source_memory_ids: list[str] = Field(default_factory=list)
    profile_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    expected_summary: str | None = None
    current_summary: str | None = None
