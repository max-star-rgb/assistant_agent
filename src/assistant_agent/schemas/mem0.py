"""Contracts for the single Mem0 memory dependency."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Mem0Identity(BaseModel):
    """Opaque Mem0-native identity filters."""

    user_id: str = Field(pattern=r"^usr_[0-9a-f]{32}$")
    agent_id: str = Field(pattern=r"^agt_[0-9a-f]{32}$")

    @property
    def mem0_filters(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
        }


class Mem0RecallRequest(BaseModel):
    identity: Mem0Identity
    top_k: int = Field(default=5, ge=1, le=50)


class Mem0MemoryRecord(BaseModel):
    engine_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    created_at: datetime | None = None
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)


class Mem0ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class Mem0TurnCaptureRequest(BaseModel):
    identity: Mem0Identity
    messages: list[Mem0ConversationMessage] = Field(
        min_length=2,
        max_length=2,
    )
    occurred_at: datetime
    source_turn: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_messages(self) -> "Mem0TurnCaptureRequest":
        if [message.role for message in self.messages] != [
            "user",
            "assistant",
        ]:
            raise ValueError(
                "turn capture requires one user and one assistant message"
            )
        return self


class Mem0TurnCaptureResult(BaseModel):
    accepted: bool
    memory_ids: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class Mem0RecallResult(BaseModel):
    records: list[Mem0MemoryRecord] = Field(default_factory=list)
    total: int = Field(ge=0)


class Mem0HealthResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str | None = None
