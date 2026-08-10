"""Mem0-specific transport models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Mem0Identity(BaseModel):
    """Opaque Mem0-native identity filters."""

    user_id: str = Field(pattern=r"^usr_[0-9a-f]{32}$")
    agent_id: str = Field(pattern=r"^agt_[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")

    @property
    def mem0_filters(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
        }

    @property
    def long_term_filters(self) -> dict[str, str]:
        return {"user_id": self.user_id, "agent_id": self.agent_id}


class Mem0CompletedTurn(BaseModel):
    """Plugin-private input for one native Mem0 ``add`` operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: Mem0Identity
    user_text: str = Field(min_length=1, max_length=20_000)
    assistant_text: str = Field(min_length=1, max_length=20_000)
    occurred_at: datetime
    source_turn: str = Field(min_length=1, max_length=512)


class Mem0MemoryChange(BaseModel):
    """One native memory mutation reported by Mem0 ``add``."""

    memory_id: str = Field(min_length=1)
    memory: str | None = None
    event: Literal["ADD", "UPDATE", "DELETE"]


class Mem0IngestionResult(BaseModel):
    accepted: bool
    memory_ids: list[str] = Field(default_factory=list)
    changes: list[Mem0MemoryChange] | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class Mem0HealthResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str | None = None
