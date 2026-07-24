"""Domain models for the long-term memory lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from assistant_agent.schemas.identity import RequestIdentity


class LongTermMemory(BaseModel):
    """One original long-term memory record returned by Mem0."""

    memory_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    created_at: datetime
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)


class SessionMemorySnapshot(BaseModel):
    """Structured long-term memories frozen for one session."""

    memories: list[LongTermMemory] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    status: str = "succeeded"


@dataclass(frozen=True)
class CompletedTurn:
    """Immutable input submitted to Mem0 for native extraction."""

    identity: RequestIdentity
    user_text: str
    assistant_text: str
    occurred_at: datetime
    source_turn: str

    @property
    def ordering_key(self) -> tuple[str, str, str]:
        return (
            self.identity.user_id,
            self.identity.agent_id,
            self.identity.session_id or "",
        )
