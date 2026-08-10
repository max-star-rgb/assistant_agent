"""Domain models for the long-term memory lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.plugins.contracts import MemoryContextItem


class LongTermMemory(MemoryContextItem):
    """One original long-term memory record returned by Mem0."""

    source: Literal["long_term"] = "long_term"


class SessionMemorySnapshot(BaseModel):
    """Structured long-term memories frozen for one session."""

    model_config = ConfigDict(extra="forbid")

    memories: list[MemoryContextItem] = Field(default_factory=list)
    plugin_id: str | None = Field(default=None, min_length=1, max_length=128)
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
