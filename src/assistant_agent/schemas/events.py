"""Agent event schemas."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.services.identifiers import new_prefixed_uuid7


EventType = Literal[
    "task_started",
    "graph_node_started",
    "graph_node_finished",
    "tool_started",
    "tool_progress",
    "tool_completed",
    "tool_finished",
    "tool_failed",
    "memory_loaded",
    "memory_saved",
    "progress_message",
    "response_delta",
    "agent_response",
    "agent_error",
    "agent_trace_decision",
    "agent_trace_observation",
    "agent_trace_final_answer",
    "final_response",
    "task_failed",
    "task_cancelled",
    "tts_started",
    "tts_finished",
    "tts_superseded",
    "display_superseded",
    "call_hangup",
]


class AgentEvent(BaseModel):
    """Structured event emitted by the agent runtime and sent over WebSocket."""

    event_id: str = Field(default_factory=lambda: new_prefixed_uuid7("event"))
    type: EventType
    session_id: str = Field(min_length=1)
    run_id: str | None = None
    node_name: str | None = None
    tool_name: str | None = None
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    output_ref: str | None = None
    text: str | None = None
    error: str | dict[str, Any] | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
