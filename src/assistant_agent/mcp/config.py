"""Configuration for inbound MCP tool adapters."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPToolAdapterConfig(BaseModel):
    """Conservative allowlist for one inbound MCP tool source."""

    server_name: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = "mcp"

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in set(self.allowed_tools)
