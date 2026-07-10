"""Tool selection, result, and call history schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.capability_output import CapabilityOutputContract

ToolSideEffectLevel = Literal[
    "none",
    "local_read",
    "external_read",
    "pending_confirmation",
    "committed",
    "compensatable",
]
ToolRisk = Literal[
    "pure",
    "local_read",
    "external_read",
    "local_write",
    "external_write",
    "transactional",
    "destructive",
]
RealtimeToolMode = Literal["inline", "blocking", "deferred", "confirm_then_execute"]
ApprovalMode = Literal["never", "conditional", "always"]
ToolIdempotencyPolicy = Literal["none", "optional", "required"]


class ToolSideEffectPolicy(BaseModel):
    """Static side-effect metadata for one tool contract."""

    level: ToolSideEffectLevel = "pending_confirmation"
    requires_confirmation: bool = True
    description: str = "Unclassified tool; treat as requiring confirmation before irreversible work."
    confirmation_kind: str | None = None
    compensation_hint: str | None = None


class RealtimeToolPolicy(BaseModel):
    """Realtime behavior metadata for one tool contract."""

    mode: RealtimeToolMode = "blocking"
    interruptible: bool = True
    commit_boundary: str | None = None


class ApprovalPolicy(BaseModel):
    """User approval metadata for one tool contract."""

    mode: ApprovalMode = "conditional"
    confirmation_kind: str | None = None


class ExecutionPolicy(BaseModel):
    """Runtime execution metadata for one tool contract."""

    timeout_s: int | None = Field(default=None, gt=0)
    retry_count: int = Field(default=0, ge=0)
    idempotency: ToolIdempotencyPolicy = "none"
    concurrency: str | None = None
    max_result_chars: int | None = Field(default=None, gt=0)


class DataPolicy(BaseModel):
    """Data handling metadata for one tool contract."""

    reads_private_data: bool = False
    writes_private_data: bool = False
    sends_data_external: bool = False
    redact_in_trace: bool = False


class VisibilityPolicy(BaseModel):
    """Tool catalog visibility metadata for one tool contract."""

    toolset: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)
    enabled_by_default: bool = True
    skill_only: bool = False


class ToolPolicyMetadata(BaseModel):
    """Declarative governance metadata for one tool contract."""

    risk: ToolRisk = "external_write"
    realtime: RealtimeToolPolicy = Field(default_factory=RealtimeToolPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    data: DataPolicy = Field(default_factory=DataPolicy)
    visibility: VisibilityPolicy = Field(default_factory=VisibilityPolicy)


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


class ToolSpec(BaseModel):
    """Provider-neutral tool description for prompts, native tool calls, and MCP views."""

    name: str = Field(min_length=1)
    description: str = Field(default="")
    input_schema: dict[str, Any] = Field(default_factory=dict)
    required_inputs: list[str] = Field(default_factory=list)
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)
    side_effect: ToolSideEffectPolicy = Field(default_factory=ToolSideEffectPolicy)
    policy: ToolPolicyMetadata | None = None


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
