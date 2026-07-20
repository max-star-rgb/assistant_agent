"""Schemas for governed MCP tool discovery."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ToolSearchInput(BaseModel):
    """Search configured MCP server tools when core tools are insufficient."""

    query: str = Field(
        default="",
        description=(
            "Capability need or missing action, such as 'send email' "
            "or 'search notion pages'."
        ),
    )
    limit: int = Field(default=8, ge=1, le=20)
    server_name: str | None = Field(
        default=None,
        description="Optional configured MCP server name to inspect.",
    )
    include_permission_required: bool = Field(
        default=True,
        description="Include allowlisted MCP tools that are registered/configured but not enabled by default.",
    )


class ToolSearchInputField(BaseModel):
    """Prompt-safe summary for one MCP tool input field."""

    name: str = Field(min_length=1)
    type: str = "string"
    required: bool = False
    description: str = ""


ToolSearchCandidateStatus = Literal["enabled", "permission_required"]


class ToolSearchCandidate(BaseModel):
    """One prompt-safe MCP tool discovery candidate."""

    tool_name: str = Field(min_length=1)
    server_name: str = Field(min_length=1)
    mcp_tool_name: str = Field(min_length=1)
    description: str = ""
    status: ToolSearchCandidateStatus
    permission_required: bool
    permission_hint: str | None = None
    read_only: bool
    side_effect_level: str
    required_inputs: list[str] = Field(default_factory=list)
    input_fields: list[ToolSearchInputField] = Field(default_factory=list)
    match_score: int = Field(default=0, ge=0)


class ToolSearchResult(BaseModel):
    """Result returned by the tool_search tool."""

    query: str = ""
    matches: list[ToolSearchCandidate] = Field(default_factory=list)
    total_matches: int = 0
    configured_server_count: int = 0
    searched_server_names: list[str] = Field(default_factory=list)
    omitted_unallowlisted_count: int = 0
    issues: list[str] = Field(default_factory=list)
    summary: str = ""
