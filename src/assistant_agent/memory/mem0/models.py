"""Mem0-specific transport models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class Mem0IngestionResult(BaseModel):
    accepted: bool
    memory_ids: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class Mem0HealthResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str | None = None
