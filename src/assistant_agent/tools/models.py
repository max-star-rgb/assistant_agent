"""Tool selection schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
