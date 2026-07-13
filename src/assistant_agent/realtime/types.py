"""Neutral realtime backend types for phone/runtime integrations."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class RealtimeCancelToken(Protocol):
    """Cooperative cancellation boundary supplied by an outer runtime."""

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""

    async def cancelled(self) -> None:
        """Wait until cancellation has been requested."""


class RealtimeAgentRequest(BaseModel):
    """Normalized request for one realtime agent turn."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str | None = None
    turn_id: str | None = None
    text: str = ""
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


RealtimeAgentEventType = Literal[
    "run.progress",
    "confirmation.required",
    "tool.started",
    "tool.finished",
    "tool.failed",
    "trace.decision",
    "trace.observation",
    "response.chunk",
    "response.final",
    "error",
]


class RealtimeAgentEvent(BaseModel):
    """Provider-neutral event emitted during a realtime agent turn."""

    type: RealtimeAgentEventType
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    display_only: bool = False
    content_type: str = "text"


RealtimeAgentStatus = Literal["completed", "cancelled", "error"]


class RealtimeAgentResult(BaseModel):
    """Final result for a realtime agent turn."""

    status: RealtimeAgentStatus
    response_text: str = ""
    expects_reply: bool = False
    run_id: str | None = None
    trace_id: str | None = None
    output_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RealtimeBackendCapabilities(BaseModel):
    """Declared capabilities for a realtime backend implementation."""

    supports_token_streaming: bool = False
    supports_tool_event_streaming: bool = True
    supports_best_effort_cancel: bool = True
    supports_hard_cancel: bool = False
    supports_multimodal_refs: bool = True
