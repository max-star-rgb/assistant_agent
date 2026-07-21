"""Shared contracts for trusted in-process tool capability plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

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

    plugin_id: str

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        """Create the tools contributed by this capability bundle."""
