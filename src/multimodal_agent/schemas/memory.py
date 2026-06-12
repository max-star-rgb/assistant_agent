"""Memory schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


MemoryType = Literal[
    "conversation",
    "video",
    "product",
    "preference",
    "task",
    "generation",
    "render",
]


class MemoryItem(BaseModel):
    """A retrievable memory item with an explainable match score."""

    memory_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    memory_type: MemoryType
    content: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(min_length=1)
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None
    created_at: datetime


class MemoryQuery(BaseModel):
    """Query options for local memory retrieval."""

    user_id: str = Field(min_length=1)
    session_id: str | None = None
    query: str = ""
    memory_types: list[MemoryType] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=50)
    max_context_chars: int = Field(default=500, ge=50, le=4000)
    since: datetime | None = None
