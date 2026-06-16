"""Tool selection, result, and call history schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.capability_output import CapabilityOutputContract


class ToolSelection(BaseModel):
    """A tool chosen by the agent for a planned step."""

    tool_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None


class ToolResult(BaseModel):
    """Structured output from a tool execution."""

    tool_name: str = Field(min_length=1)
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    output_ref: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    contract: CapabilityOutputContract | None = None


class ToolCallRecord(BaseModel):
    """Persistent record for one tool invocation."""

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    output_ref: str | None = None
    error_message: str | None = None
