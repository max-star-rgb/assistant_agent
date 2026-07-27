"""Platform-neutral trace context accepted at the Assistant runtime boundary."""

from pydantic import BaseModel, ConfigDict, Field


class RuntimeTraceContext(BaseModel):
    """External W3C identity used to nest Runtime under an experiment span."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_span_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
    )
