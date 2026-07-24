"""Session/thread schemas."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    """Create a new user conversation session."""

    user_id: str = Field(min_length=1)
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(BaseModel):
    """A user-owned conversation thread index record."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_count: int = Field(default=0, ge=0)
    last_run_id: str | None = None
    last_trace_id: str | None = None
    last_message_preview: str = ""
    last_status: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionList(BaseModel):
    """List user sessions."""

    user_id: str = Field(min_length=1)
    total: int = Field(ge=0)
    sessions: list[SessionRecord] = Field(default_factory=list)


class SessionDeleteResult(BaseModel):
    """Delete result for session/thread records."""

    user_id: str = Field(min_length=1)
    deleted: dict[str, int] = Field(default_factory=dict)
