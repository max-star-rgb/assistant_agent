"""Prompt-facing projection of Mem0 records."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemoryPromptSnapshot(BaseModel):
    """Long-term memory frozen when a session starts."""

    schema_version: Literal["memory_prompt_snapshot_v1"] = (
        "memory_prompt_snapshot_v1"
    )
    text: str = ""
    source_ids: list[str] = Field(default_factory=list, max_length=50)


class MemoryItem(BaseModel):
    """Minimal Mem0 record needed by the runtime context."""

    memory_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    created_at: datetime
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
