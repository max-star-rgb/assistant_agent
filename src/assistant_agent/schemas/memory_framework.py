"""Governed contracts shared by external memory-framework adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.memory import MemoryRecordKind, MemoryScope, MemoryType


MemoryFrameworkName = Literal["hindsight", "mem0"]

_UNSAFE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "base64",
    "bearer",
    "cookie",
    "password",
    "provider_response",
    "raw",
    "raw_audio",
    "raw_image",
    "raw_media",
    "raw_payload",
    "raw_provider_payload",
    "raw_provider_response",
    "raw_video",
    "secret",
    "token",
}

_ENGINE_AGENT_SCOPES = {"project", "task", "video", "product"}


class MemoryEngineIdentity(BaseModel):
    """Opaque, stable identifiers bound from trusted RequestIdentity.

    Mem0 exposes only `user_id`, `agent_id`, and `run_id` as identity filters.
    `agent_id` is the framework work-domain id derived from assistant_agent
    `project_id`; it is not an assistant_agent multi-agent identifier.
    """

    bank_id: str = Field(pattern=r"^bank_[0-9a-f]{32}$")
    user_id: str = Field(pattern=r"^usr_[0-9a-f]{32}$")
    agent_id: str = Field(pattern=r"^agt_[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    tenant_tag: str = Field(pattern=r"^tenant_[0-9a-f]{24}$")
    user_tag: str = Field(pattern=r"^user_[0-9a-f]{24}$")
    project_tag: str = Field(pattern=r"^project_[0-9a-f]{24}$")
    session_tag: str = Field(pattern=r"^session_[0-9a-f]{24}$")

    @property
    def hindsight_tags(self) -> list[str]:
        return [self.tenant_tag, self.user_tag, self.project_tag, self.session_tag]

    def hindsight_tags_for_scope(self, scope: MemoryScope | str | None) -> list[str]:
        resolved = scope or "session"
        if resolved == "user_profile":
            return [self.tenant_tag, self.user_tag]
        if resolved in _ENGINE_AGENT_SCOPES:
            return [self.tenant_tag, self.user_tag, self.project_tag]
        return self.hindsight_tags

    @property
    def mem0_filters(self) -> dict[str, str]:
        return {"user_id": self.user_id, "agent_id": self.agent_id, "run_id": self.run_id}

    def mem0_filters_for_scope(self, scope: MemoryScope | str | None) -> dict[str, str]:
        """Return only Mem0-native identity filters for the requested memory scope."""

        resolved = scope or "session"
        if resolved == "user_profile":
            return {"user_id": self.user_id}
        if resolved in _ENGINE_AGENT_SCOPES:
            return {"user_id": self.user_id, "agent_id": self.agent_id}
        return self.mem0_filters


class FrameworkRetainRequest(BaseModel):
    identity: MemoryEngineIdentity
    project_memory_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=20_000)
    memory_type: MemoryType
    scope: MemoryScope = "session"
    source: str = Field(min_length=1, max_length=128)
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _reject_unsafe(self) -> "FrameworkRetainRequest":
        _reject_unsafe_payload(self.metadata)
        _reject_unsafe_payload(self.text)
        return self


class FrameworkRecallRequest(BaseModel):
    identity: MemoryEngineIdentity
    query: str = Field(min_length=1, max_length=4000)
    scope: MemoryScope = "session"
    top_k: int = Field(default=5, ge=1, le=50)
    memory_types: list[MemoryType] = Field(default_factory=list)
    since: datetime | None = None
    max_tokens: int = Field(default=1000, ge=64, le=8192)
    record_kinds: list[MemoryRecordKind] = Field(default_factory=list)


class FrameworkMemoryRecord(BaseModel):
    engine_id: str = Field(min_length=1)
    project_memory_id: str | None = None
    text: str = Field(min_length=1)
    memory_type: MemoryType = "task"
    source: str = "memory_framework"
    created_at: datetime | None = None
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    record_kind: MemoryRecordKind = "legacy"

    @model_validator(mode="after")
    def _reject_unsafe(self) -> "FrameworkMemoryRecord":
        _reject_unsafe_payload(self.metadata)
        _reject_unsafe_payload(self.text)
        return self


class FrameworkRetainResult(BaseModel):
    accepted: bool
    engine_ids: list[str] = Field(default_factory=list)
    operation_id: str | None = None


class FrameworkConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class FrameworkTurnCaptureRequest(BaseModel):
    """One completed turn submitted to a framework-owned memory lifecycle."""

    identity: MemoryEngineIdentity
    messages: list[FrameworkConversationMessage] = Field(min_length=2, max_length=2)
    daily_text: str = Field(min_length=1, max_length=20_000)
    daily_memory_id: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _reject_unsafe(self) -> "FrameworkTurnCaptureRequest":
        if [message.role for message in self.messages] != ["user", "assistant"]:
            raise ValueError("turn capture messages must be one user message followed by one assistant message")
        _reject_unsafe_payload(self.metadata)
        _reject_unsafe_payload(self.daily_text)
        for message in self.messages:
            _reject_unsafe_payload(message.content)
        return self


class FrameworkTurnCaptureResult(BaseModel):
    accepted: bool
    daily_engine_ids: list[str] = Field(default_factory=list)
    core_engine_ids: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class FrameworkRecallResult(BaseModel):
    records: list[FrameworkMemoryRecord] = Field(default_factory=list)
    total: int = Field(ge=0)


class FrameworkHealthResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str | None = None


def _reject_unsafe_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in _UNSAFE_KEYS:
                raise ValueError(f"unsafe framework memory payload key: {key}")
            _reject_unsafe_payload(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_unsafe_payload(nested)
        return
    if isinstance(value, str) and value.strip().lower().startswith(("data:image/", "data:audio/", "data:video/")):
        raise ValueError("unsafe framework memory payload contains inline media")
