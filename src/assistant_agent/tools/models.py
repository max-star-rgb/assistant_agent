"""Tool selection, result, and call history schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.tools.capability_output import CapabilityOutputContract

ToolCategory = Literal["read", "generate", "write", "dangerous"]
ToolMediaRequirement = Literal["video", "image", "audio"]
ToolMediaScope = Literal["any", "attached", "live"]
ToolRepeatPolicy = Literal["once_per_run", "distinct_inputs"]


def _empty_tool_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
    }


class ToolSelection(BaseModel):
    """A tool chosen by the agent for a planned step."""

    tool_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None


class ToolResult(BaseModel):
    """Structured output from a tool execution.

    ``data`` is the full runtime/API/trace payload. ``model_observation`` is the
    tool-owned projection that the assistant loop may expose back to the main
    LLM for reasoning and final-answer synthesis.
    """

    tool_name: str = Field(min_length=1)
    success: bool
    data: dict[str, Any] | None = None
    voice_summary: str | None = None
    model_observation: dict[str, Any] | None = None
    trace_summary: dict[str, Any] | None = None
    audit_payload: dict[str, Any] | None = None
    raw_data_ref: str | None = None
    error: str | None = None
    output_ref: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    contract: CapabilityOutputContract | None = None


class ToolSpec(BaseModel):
    """Single provider-neutral contract for exposure, validation, and execution."""

    name: str = Field(min_length=1)
    description: str = Field(default="")
    input_schema: dict[str, Any] = Field(default_factory=_empty_tool_input_schema)
    category: ToolCategory = "dangerous"
    requires_media: list[ToolMediaRequirement] = Field(default_factory=list)
    media_scope: ToolMediaScope = "any"
    repeat_policy: ToolRepeatPolicy = "once_per_run"


class RunToolCatalog(BaseModel):
    """The tools exposed to—and therefore callable by—the model for one turn."""

    schema_version: Literal["run_tool_catalog_v1"] = "run_tool_catalog_v1"
    available_tool_names: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    excluded_reasons: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_available_tools(self) -> "RunToolCatalog":
        if len(self.available_tool_names) != len(set(self.available_tool_names)):
            raise ValueError("available_tool_names must not contain duplicates")
        return self

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.available_tool_names


class ToolCallRecord(BaseModel):
    """Persistent record for one tool invocation."""

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    output_ref: str | None = None
    error_message: str | None = None
