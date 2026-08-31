"""Static LangChain Tool and official MCP assembly for the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any

from langchain_core.tools import BaseTool

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.amap_route_links import amap_route_link_interceptor
from assistant_agent.mcp.config import (
    MCPServerConfig,
)
from assistant_agent.mcp.stateful_sessions import (
    StatefulMcpInterceptor,
    ThreadMcpSessionPool,
    resolve_mcp_connection,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


GENERAL_PURPOSE_BUILTIN_TOOL_NAMES = frozenset(
    {
        "calendar_search",
        "contacts_search",
        "email_read",
        "email_search",
        "file_read",
        "live_view_inspect",
        "lodging_search",
        "shopping_search",
        "uploaded_media_inspect",
        "visual_image_search",
        "visual_memory_search",
        "web_fetch",
        "web_search",
    }
)
INTERRUPT_BUILTIN_TOOL_NAMES = frozenset(
    {
        "calendar_create",
        "hotel_price_watch_create",
        "image_generation",
        "image_to_3d",
        "visual_reminder_manage",
    }
)


@dataclass(frozen=True)
class NativeToolResources:
    """Optional process resources consumed by explicitly installed built-ins."""

    video_context_store: Any | None = None
    vision_client: Any | None = None
    realtime_video_memory_store: Any | None = None
    durable_task_service: Any | None = None
    calendar_adapter: Any | None = None
    embedding_coordinator_store: Any | None = None
    visual_semantic_store_pool: Any | None = None
    visual_reminder_registry: Any | None = None
    visual_memory_text_index: Any | None = None
    visual_history_probe: Any | None = None
    live_view_resolver: Callable[[str, str, str], Any] | None = None
    thread_resource_manager: Any | None = None


def _create_builtin_tools(
    config: ProviderConfig,
    *,
    resources: NativeToolResources,
) -> list[BaseTool]:
    """Build the trusted in-process inventory without Registry or discovery."""

    context = ToolPluginContext(
        config=config,
        video_context_store=resources.video_context_store,
        vision_client=resources.vision_client,
        realtime_video_memory_store=resources.realtime_video_memory_store,
        durable_task_service=resources.durable_task_service,
        calendar_adapter=resources.calendar_adapter,
        embedding_coordinator_store=resources.embedding_coordinator_store,
        visual_semantic_store_pool=resources.visual_semantic_store_pool,
        visual_reminder_registry=resources.visual_reminder_registry,
        visual_memory_text_index=resources.visual_memory_text_index,
        live_view_resolver=resources.live_view_resolver,
        thread_resource_manager=resources.thread_resource_manager,
    )
    concrete_tools: list[BaseTool] = []
    for plugin in _builtin_plugins():
        built = plugin.build_tools(context)
        if not all(isinstance(tool, BaseTool) for tool in built):
            raise TypeError("built-in plugins must return LangChain BaseTool instances")
        concrete_tools.extend(built)
    names = [tool.name for tool in concrete_tools]
    if len(names) != len(set(names)):
        raise ValueError("native tool names must be unique")
    return sorted(concrete_tools, key=lambda tool: tool.name)


def mcp_connections(
    server_configs: Sequence[MCPServerConfig],
    *,
    discovery_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Translate trusted stdio config to the official adapter schema."""

    connections: dict[str, dict[str, Any]] = {}
    for server in server_configs:
        root = (
            discovery_root / server.server_name
            if discovery_root is not None and server.session_scope == "thread"
            else None
        )
        connections[server.server_name] = resolve_mcp_connection(
            server,
            discovery_root=root,
        )
    return connections


async def _create_official_mcp_tools(
    server_configs: Sequence[MCPServerConfig],
    *,
    client_factory: Callable[..., Any] | None = None,
    mcp_session_pool: ThreadMcpSessionPool | None = None,
) -> list[BaseTool]:
    """Load allowlisted MCP tools through MultiServerMCPClient."""

    if not server_configs:
        return []
    if client_factory is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client_factory = MultiServerMCPClient
    if any(server.session_scope == "thread" for server in server_configs):
        if mcp_session_pool is None:
            raise ValueError("thread-scoped MCP requires a session pool")
        interceptors = [
            StatefulMcpInterceptor(server_configs, mcp_session_pool),
            amap_route_link_interceptor,
        ]
    else:
        interceptors = [amap_route_link_interceptor]
    temporary = await asyncio.to_thread(
        tempfile.TemporaryDirectory,
        prefix="assistant-agent-mcp-discovery-",
    )
    try:
        connections = await asyncio.to_thread(
            mcp_connections,
            server_configs,
            discovery_root=Path(temporary.name),
        )
        client = client_factory(
            connections,
            tool_interceptors=interceptors,
            tool_name_prefix=False,
        )
        assembled: list[BaseTool] = []
        for server in server_configs:
            discovered = await client.get_tools(server_name=server.server_name)
            allowed = set(server.allowed_tools)
            for tool in discovered:
                if tool.name not in allowed:
                    continue
                name = f"{server.namespace_prefix}_{server.server_name}_{tool.name}"
                assembled.append(
                    tool.model_copy(
                        update={
                            "name": name,
                            "metadata": {
                                **(tool.metadata or {}),
                                "source": "mcp",
                                "mcp_server": server.server_name,
                            },
                        }
                    )
                )
    finally:
        await asyncio.to_thread(temporary.cleanup)
    names = [tool.name for tool in assembled]
    if len(names) != len(set(names)):
        raise ValueError("namespaced MCP tool names must be unique")
    return assembled


async def create_native_tool_inventory(
    config: ProviderConfig,
    *,
    resources: NativeToolResources,
    mcp_server_configs: Sequence[MCPServerConfig],
    mcp_client_factory: Callable[..., Any] | None = None,
    mcp_session_pool: ThreadMcpSessionPool | None = None,
) -> list[BaseTool]:
    """Compose the one production inventory from built-ins and official MCP tools."""

    builtins = await asyncio.to_thread(
        _create_builtin_tools,
        config,
        resources=resources,
    )
    mcp_tools = await _create_official_mcp_tools(
        mcp_server_configs,
        client_factory=mcp_client_factory,
        mcp_session_pool=mcp_session_pool,
    )
    tools = [*builtins, *mcp_tools]
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("native and MCP tool names must be unique")
    return sorted(tools, key=lambda tool: tool.name)


def general_purpose_tool_names(
    tools: Sequence[BaseTool],
    server_configs: Sequence[MCPServerConfig],
) -> frozenset[str]:
    """Return exact Tool grants for the preassembled general-purpose role."""

    configured = {
        f"{server.namespace_prefix}_{server.server_name}_{name}"
        for server in server_configs
        for name in server.general_purpose_tools
    }
    available = {tool.name for tool in tools}
    return frozenset((GENERAL_PURPOSE_BUILTIN_TOOL_NAMES | configured) & available)


def interrupt_tool_names(
    tools: Sequence[BaseTool],
    server_configs: Sequence[MCPServerConfig],
) -> frozenset[str]:
    """Return exact Tool names explicitly governed by native HITL."""

    configured = {
        f"{server.namespace_prefix}_{server.server_name}_{name}"
        for server in server_configs
        for name in server.interrupt_tools
    }
    available = {tool.name for tool in tools}
    return frozenset((INTERRUPT_BUILTIN_TOOL_NAMES | configured) & available)


def _builtin_plugins() -> tuple[Any, ...]:
    """Return an explicit list; no filesystem or configured-module discovery."""

    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.plugin import (
        CalendarContactsPlugin,
    )
    from assistant_agent.tools.plugins.builtin.email_access.plugin import (
        EmailAccessPlugin,
    )
    from assistant_agent.tools.plugins.builtin.image_generation.plugin import (
        ImageGenerationToolPlugin,
    )
    from assistant_agent.tools.plugins.builtin.image_to_3d.plugin import (
        ImageTo3DToolPlugin,
    )
    from assistant_agent.tools.plugins.builtin.lodging.plugin import LodgingToolPlugin
    from assistant_agent.tools.plugins.builtin.media_inspection.plugin import (
        MediaInspectionPlugin,
    )
    from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
    from assistant_agent.tools.plugins.builtin.visual_image_search.plugin import (
        VisualImageSearchPlugin,
    )

    return (
        EmailAccessPlugin(),
        LodgingToolPlugin(),
        MediaInspectionPlugin(),
        VisualImageSearchPlugin(),
        ShoppingToolPlugin(),
        CalendarContactsPlugin(),
        ImageGenerationToolPlugin(),
        ImageTo3DToolPlugin(),
    )


__all__ = [
    "GENERAL_PURPOSE_BUILTIN_TOOL_NAMES",
    "INTERRUPT_BUILTIN_TOOL_NAMES",
    "NativeToolResources",
    "general_purpose_tool_names",
    "interrupt_tool_names",
    "create_native_tool_inventory",
    "mcp_connections",
]
