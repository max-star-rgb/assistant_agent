"""Strong, prompt-safe contracts for Assistant Memory Plugins."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _FrozenMemoryModel(BaseModel):
    """Base model for immutable plugin-facing values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )


@runtime_checkable
class MemoryCancellationToken(Protocol):
    """Cooperative cancellation handle owned by the Memory Plugin Host."""

    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


@dataclass(frozen=True)
class NeverCancelledMemoryToken:
    """Cancellation token suitable for offline and no-cancellation callers."""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class MemoryPluginCapabilities(_FrozenMemoryModel):
    modalities: set[Literal["text", "image", "audio", "video", "document"]]
    supports_session_recall: bool
    supports_turn_ingestion: bool
    supports_context_refresh: bool
    supports_idempotent_ingestion: bool


class MemoryPluginDescriptor(_FrozenMemoryModel):
    plugin_id: str = Field(min_length=1, max_length=128)
    plugin_version: str = Field(min_length=1, max_length=128)
    api_version: Literal["assistant_memory_plugin_v1"] = "assistant_memory_plugin_v1"
    kind: Literal["memory"] = "memory"
    capabilities: MemoryPluginCapabilities


class MemoryIdentity(_FrozenMemoryModel):
    user_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    tenant_id: str | None = Field(default=None, max_length=512)
    project_id: str | None = Field(default=None, max_length=512)


class MemoryMessage(_FrozenMemoryModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=20_000)


class MemoryToolEvidence(_FrozenMemoryModel):
    tool_name: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "partial"]
    output_ref: str | None = Field(default=None, max_length=512)


class MemoryBudgetHint(_FrozenMemoryModel):
    max_items: int = Field(ge=0)
    max_chars: int = Field(ge=0)


class ManagedMediaRef(_FrozenMemoryModel):
    ref_id: str = Field(min_length=1, max_length=512)
    media_type: Literal["image", "audio", "video", "document"]
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    created_at: datetime
    owner_scope: str = Field(min_length=1, max_length=512)


class MemoryContextItem(_FrozenMemoryModel):
    """Prompt-safe memory evidence, never a role message or prompt patch."""

    memory_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=20_000)
    source: Literal[
        "long_term",
        "episodic",
        "semantic",
        "visual",
        "audio",
        "document",
    ]
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    media_refs: list[ManagedMediaRef] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class MemoryPluginIssue(_FrozenMemoryModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    recoverable: bool
    retry_after_seconds: float | None = Field(default=None, ge=0.0)


class MemoryContextContribution(_FrozenMemoryModel):
    items: list[MemoryContextItem] = Field(default_factory=list)
    status: Literal["succeeded", "partial", "unavailable"]
    issues: list[MemoryPluginIssue] = Field(default_factory=list)


class MemorySessionOpenRequest(_FrozenMemoryModel):
    memory_session_id: str = Field(min_length=1, max_length=512)
    identity: MemoryIdentity
    opened_at: datetime
    entry_profile: str = Field(min_length=1, max_length=128)
    deadline: datetime
    cancellation: MemoryCancellationToken = Field(exclude=True)


class MemorySessionOpenResult(_FrozenMemoryModel):
    status: Literal["ready", "degraded", "unavailable"]
    session_handle: str | None = Field(default=None, max_length=512)
    initial_contribution: MemoryContextContribution | None = None
    issues: list[MemoryPluginIssue] = Field(default_factory=list)


class MemoryContextRequest(_FrozenMemoryModel):
    memory_session_id: str = Field(min_length=1, max_length=512)
    session_handle: str | None = Field(default=None, max_length=512)
    identity: MemoryIdentity
    current_turn: MemoryMessage
    media_refs: list[ManagedMediaRef] = Field(default_factory=list)
    context_budget_hint: MemoryBudgetHint
    deadline: datetime
    cancellation: MemoryCancellationToken = Field(exclude=True)


class CompletedMemoryTurn(_FrozenMemoryModel):
    user_message: MemoryMessage
    assistant_message: MemoryMessage
    tool_evidence: list[MemoryToolEvidence] = Field(default_factory=list)
    media_refs: list[ManagedMediaRef] = Field(default_factory=list)
    occurred_at: datetime


class MemoryChange(_FrozenMemoryModel):
    operation: Literal["created", "updated", "deleted", "unchanged"]
    memory_id: str = Field(min_length=1, max_length=512)
    memory_type: str | None = Field(default=None, max_length=128)


class MemoryTurnIngestionRequest(_FrozenMemoryModel):
    memory_session_id: str = Field(min_length=1, max_length=512)
    session_handle: str | None = Field(default=None, max_length=512)
    identity: MemoryIdentity
    turn: CompletedMemoryTurn
    idempotency_key: str = Field(min_length=1, max_length=512)
    deadline: datetime
    cancellation: MemoryCancellationToken = Field(exclude=True)


class MemoryTurnIngestionResult(_FrozenMemoryModel):
    status: Literal["accepted", "partial", "rejected", "failed"]
    changes: list[MemoryChange] = Field(default_factory=list)
    issues: list[MemoryPluginIssue] = Field(default_factory=list)


class MemorySessionCloseRequest(_FrozenMemoryModel):
    memory_session_id: str = Field(min_length=1, max_length=512)
    session_handle: str | None = Field(default=None, max_length=512)
    identity: MemoryIdentity
    reason: Literal["normal", "reset", "expired", "shutdown", "plugin_replaced"]
    deadline: datetime
    cancellation: MemoryCancellationToken = Field(exclude=True)


class MemorySessionCloseResult(_FrozenMemoryModel):
    status: Literal["closed", "partial", "failed"]
    issues: list[MemoryPluginIssue] = Field(default_factory=list)


class MemoryMediaReader(Protocol):
    def read(self, ref: ManagedMediaRef, *, max_bytes: int) -> bytes: ...

    def open_stream(self, ref: ManagedMediaRef, *, max_bytes: int) -> BinaryIO: ...


class MemoryArtifactPayload(Protocol):
    """Opaque payload accepted only by a host-owned artifact writer."""


class MemoryArtifactWriter(Protocol):
    def register(self, payload: MemoryArtifactPayload) -> ManagedMediaRef: ...


class MemorySecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


@dataclass(frozen=True)
class MemoryPluginBuildContext:
    provider_mode: Literal["mock", "real"]
    media_reader: MemoryMediaReader
    artifact_writer: MemoryArtifactWriter
    secret_resolver: MemorySecretResolver
    clock: Callable[[], datetime]


class MemoryPlugin(Protocol):
    descriptor: MemoryPluginDescriptor

    def open_session(
        self,
        request: MemorySessionOpenRequest,
    ) -> MemorySessionOpenResult: ...

    def prepare_context(
        self,
        request: MemoryContextRequest,
    ) -> MemoryContextContribution: ...

    def ingest_turn(
        self,
        request: MemoryTurnIngestionRequest,
    ) -> MemoryTurnIngestionResult: ...

    def close_session(
        self,
        request: MemorySessionCloseRequest,
    ) -> MemorySessionCloseResult: ...


class MemoryPluginFactory(Protocol):
    descriptor: MemoryPluginDescriptor
    config_model: type[BaseModel]

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> MemoryPlugin: ...


class MemoryPluginExecutionPolicy(_FrozenMemoryModel):
    open_session_timeout_seconds: float = Field(default=5.0, gt=0.0)
    prepare_context_timeout_seconds: float = Field(default=5.0, gt=0.0)
    ingest_turn_timeout_seconds: float = Field(default=30.0, gt=0.0)
    close_session_timeout_seconds: float = Field(default=5.0, gt=0.0)
    max_context_items: int = Field(default=10_000, ge=0)
    max_context_chars: int = Field(default=2_000_000, ge=0)
    max_media_items_per_turn: int = Field(default=16, ge=0)
    max_media_bytes_per_turn: int = Field(default=32 * 1024 * 1024, ge=0)
