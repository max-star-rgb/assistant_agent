"""Platform-neutral trace contexts accepted at the Assistant runtime boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeTraceContext(BaseModel):
    """External W3C identity used to nest Runtime under an experiment span."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_span_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
    )


class RuntimeExportTraceContext(BaseModel):
    """Export-only identity for fusing one run into its Workflow attempt."""

    model_config = ConfigDict(frozen=True)

    export_trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    export_span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    export_parent_span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    export_trace_name: str = Field(min_length=1, max_length=160)
    export_observation_name: str = Field(min_length=1, max_length=240)
    execution_role: Literal["workflow_work_item"] = "workflow_work_item"
    workflow_id: str = Field(min_length=1, max_length=160)
    work_item_id: str = Field(min_length=1, max_length=160)
    attempt_id: str = Field(min_length=1, max_length=160)
    agent_role: Literal["planner", "worker"] = "worker"
