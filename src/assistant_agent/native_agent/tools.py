"""Static LangChain Tool and official MCP assembly for the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.amap_route_links import amap_route_link_interceptor
from assistant_agent.mcp.config import (
    MCPServerConfig,
    resolve_mcp_server_env,
)
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.plugins.contracts import ToolPluginContext


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


def _create_builtin_tools(
    config: ProviderConfig,
    *,
    resources: NativeToolResources,
    skill_catalog: SkillCatalog,
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
    )
    concrete_tools: list[BaseTool] = []
    for plugin in _builtin_plugins(skill_catalog=skill_catalog):
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
) -> dict[str, dict[str, Any]]:
    """Translate trusted stdio config to the official adapter schema."""

    connections: dict[str, dict[str, Any]] = {}
    for server in server_configs:
        command, *args = server.command
        connection: dict[str, Any] = {
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": resolve_mcp_server_env(server.env),
        }
        if server.cwd is not None:
            connection["cwd"] = server.cwd
        connections[server.server_name] = connection
    return connections


async def _create_official_mcp_tools(
    server_configs: Sequence[MCPServerConfig],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> list[BaseTool]:
    """Load allowlisted MCP tools through MultiServerMCPClient."""

    if not server_configs:
        return []
    if client_factory is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client_factory = MultiServerMCPClient
    client = client_factory(
        mcp_connections(server_configs),
        tool_interceptors=[amap_route_link_interceptor],
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
                            "effect": (
                                "read"
                                if tool.name in set(server.read_only_tools)
                                else "dangerous"
                            ),
                            "source": "mcp",
                            "mcp_server": server.server_name,
                        },
                    }
                )
            )
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
    skill_catalog: SkillCatalog,
) -> list[BaseTool]:
    """Compose the one production inventory from built-ins and official MCP tools."""

    builtins = await asyncio.to_thread(
        _create_builtin_tools,
        config,
        resources=resources,
        skill_catalog=skill_catalog,
    )
    mcp_tools = await _create_official_mcp_tools(
        mcp_server_configs,
        client_factory=mcp_client_factory,
    )
    tools = [*builtins, *mcp_tools]
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("native and MCP tool names must be unique")
    return sorted(tools, key=lambda tool: tool.name)


def _builtin_plugins(
    *,
    skill_catalog: SkillCatalog,
) -> tuple[Any, ...]:
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
    from assistant_agent.tools.plugins.builtin.local_file_access.plugin import (
        LocalFileAccessPlugin,
    )
    from assistant_agent.tools.plugins.builtin.lodging.plugin import LodgingToolPlugin
    from assistant_agent.tools.plugins.builtin.media_inspection.plugin import (
        MediaInspectionPlugin,
    )
    from assistant_agent.tools.plugins.builtin.python_execution.plugin import (
        PythonExecutionPlugin,
    )
    from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
    from assistant_agent.tools.plugins.builtin.skill_loading.plugin import (
        SkillLoadingPlugin,
    )
    from assistant_agent.tools.plugins.builtin.visual_image_search.plugin import (
        VisualImageSearchPlugin,
    )
    from assistant_agent.tools.plugins.builtin.website_guidance.plugin import (
        WebsiteGuidancePlugin,
    )

    return (
        EmailAccessPlugin(),
        LocalFileAccessPlugin(),
        SkillLoadingPlugin(skill_catalog=skill_catalog),
        LodgingToolPlugin(),
        PythonExecutionPlugin(),
        MediaInspectionPlugin(),
        VisualImageSearchPlugin(),
        WebsiteGuidancePlugin(),
        ShoppingToolPlugin(),
        CalendarContactsPlugin(),
        ImageGenerationToolPlugin(),
        ImageTo3DToolPlugin(),
    )


__all__ = [
    "NativeToolResources",
    "create_native_tool_inventory",
    "mcp_connections",
]
