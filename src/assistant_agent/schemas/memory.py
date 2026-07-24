"""Runtime-facing projection of Mem0 records."""

from datetime import datetime

from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    """Minimal Mem0 record needed by the runtime context."""

    memory_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    created_at: datetime
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
