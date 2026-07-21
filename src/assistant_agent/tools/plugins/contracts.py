"""Shared contracts for trusted in-process tool capability plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.base import Tool

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
    from assistant_agent.services.durable_tasks.service import DurableTaskService
    from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
    from assistant_agent.services.video_context import VideoContextStore


@dataclass(frozen=True)
class ToolPluginContext:
    """Dependencies and structured enablement facts available to built-in plugins."""

    config: ProviderConfig
    mcp_server_configs: list[MCPServerConfig]
    mcp_runner: MCPToolDiscoveryRunner | None = None
    video_context_store: VideoContextStore | None = None
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None
    durable_task_service: DurableTaskService | None = None

    @property
    def mock_mode(self) -> bool:
        return self.config.provider_mode == "mock"


class ToolPlugin(Protocol):
    """A capability bundle that constructs tools but cannot bypass governance."""

    descriptor: "ToolPluginDescriptor"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        """Create the tools contributed by this capability bundle."""


class ToolPluginDescriptor(BaseModel):
    """Versioned identity declared by one in-process Tool plugin."""

    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._]*$")
    plugin_version: str = Field(min_length=1)
    api_version: Literal["tool_plugin_v1"] = "tool_plugin_v1"


class ToolPluginSourceRecord(BaseModel):
    """Host-owned provenance for a Tool source."""

    source_type: Literal["builtin", "configured_module", "mcp", "manual", "realtime_observer"]
    source_ref: str = Field(min_length=1)
    trusted: bool


class ToolPluginLoadIssue(BaseModel):
    """Sanitized issue produced during discovery or atomic assembly."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    plugin_id: str | None = None
    tool_name: str | None = None


class ToolRegistrationRecord(BaseModel):
    """Safe ownership metadata retained by a finalized ToolRegistry."""

    tool_name: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    plugin_version: str = Field(min_length=1)
    source_type: Literal["builtin", "configured_module", "mcp", "manual", "realtime_observer"]
    source_ref: str = Field(min_length=1)


class ToolPluginAssemblyReport(BaseModel):
    """Read-only startup diagnostics; Tool objects are intentionally excluded."""

    schema_version: Literal["tool_plugin_assembly_v1"] = "tool_plugin_assembly_v1"
    sources: list[ToolPluginSourceRecord] = Field(default_factory=list)
    registrations: list[ToolRegistrationRecord] = Field(default_factory=list)
    issues: list[ToolPluginLoadIssue] = Field(default_factory=list)


@dataclass(frozen=True)
class LoadedToolPlugin:
    """A validated plugin paired with host-owned provenance."""

    plugin: Any
    descriptor: ToolPluginDescriptor
    source: ToolPluginSourceRecord
