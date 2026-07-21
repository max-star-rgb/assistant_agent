"""Registration helpers for explicitly configured external MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from assistant_agent.mcp.adapter import (
    MCPToolAdapter,
    MCPToolDefinition,
    MCPToolRunner,
)
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.services.provider_errors import sanitize_error_message


class MCPToolDiscoveryRunner(MCPToolRunner, Protocol):
    """Boundary used to discover and execute tools from one MCP server."""

    def list_tools(self, *, server: MCPServerConfig) -> list[MCPToolDefinition]:
        """Return provider-advertised tool definitions for one configured server."""


class MCPToolRegistrationSummary(BaseModel):
    """Compact registration report for diagnostics and tests."""

    registered_tool_names: list[str] = Field(default_factory=list)
    skipped_tool_names: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class MCPDiscoveredTool:
    """A validated allowlisted MCP proxy plus its host-owned source identity."""

    tool: Any
    server_name: str


def discover_configured_mcp_tools(
    server_configs: list[MCPServerConfig],
    *,
    runner: MCPToolDiscoveryRunner | None = None,
) -> tuple[list[MCPDiscoveredTool], MCPToolRegistrationSummary]:
    """Discover allowlisted proxies without mutating a Registry."""

    summary = MCPToolRegistrationSummary()
    discovered: list[MCPDiscoveredTool] = []
    if not server_configs:
        return discovered, summary
    discovery_runner = runner
    if discovery_runner is None:
        try:
            from assistant_agent.mcp.sdk_client import SdkMCPClientRunner

            discovery_runner = SdkMCPClientRunner(server_configs)
        except ImportError:
            from assistant_agent.mcp.stdio_client import StdioMCPClientRunner

            discovery_runner = StdioMCPClientRunner(server_configs)

    for server in server_configs:
        adapter = MCPToolAdapter(server.adapter_config(), runner=discovery_runner)
        try:
            definitions = discovery_runner.list_tools(server=server)
        except Exception as exc:  # pragma: no cover - defensive discovery boundary
            summary.issues.append(f"{server.server_name}: {sanitize_error_message(exc)}")
            continue
        for definition in definitions:
            tool_name = adapter.namespaced_tool_name(definition.name)
            if not server.adapter_config().is_allowed(definition.name):
                summary.skipped_tool_names.append(tool_name)
                continue
            try:
                proxy = adapter.proxy_tool_for_definition(definition)
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                summary.issues.append(f"{tool_name}: {sanitize_error_message(exc)}")
                continue
            discovered.append(MCPDiscoveredTool(tool=proxy, server_name=server.server_name))
            summary.registered_tool_names.append(tool_name)
    return discovered, summary


def register_configured_mcp_tools(
    registry: object,
    server_configs: list[MCPServerConfig],
    *,
    runner: MCPToolDiscoveryRunner | None = None,
) -> MCPToolRegistrationSummary:
    """Discover allowlisted external MCP tools and register proxy tools.

    This function is intentionally opt-in and only trusts explicit per-tool
    allowlists from ``MCPServerConfig``.
    """

    discovered, summary = discover_configured_mcp_tools(server_configs, runner=runner)
    register = getattr(registry, "register", None)
    if not callable(register):
        summary.issues.append("registry does not expose register(tool).")
        return summary

    registered_names: list[str] = []
    for item in discovered:
        try:
            register(item.tool)
        except Exception as exc:  # pragma: no cover - defensive registry boundary
            summary.issues.append(f"{item.tool.name}: {sanitize_error_message(exc)}")
            continue
        registered_names.append(item.tool.name)
    summary.registered_tool_names = registered_names
    return summary
