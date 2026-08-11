"""Composition roots that assemble governed Tool registries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.adapter import MCPToolRunner, namespaced_mcp_tool_name
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.video_context import VideoContextStore
from assistant_agent.tools.plugins.assembly import (
    ToolContribution,
    ToolPluginAssemblyError,
    assemble_tool_plugins,
    configured_plugin_modules_from_env,
    normalize_configured_plugin_modules,
)
from assistant_agent.tools.plugins.builtin.media_inspection.plugin import (
    build_realtime_video_observation_tool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginAssemblyReport,
    ToolPluginContext,
    ToolPluginLoadIssue,
    ToolPluginSourceRecord,
    ToolRegistrationRecord,
)
from assistant_agent.tools.plugins.defaults import default_tool_plugins
from assistant_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
        CalendarAdapter,
    )


_PROVIDER_NATIVE_WEATHER_REMOTE_TOOL_NAMES = frozenset({"maps_weather"})


def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
    durable_task_service: DurableTaskService | None = None,
    workflow_service: WorkflowService | None = None,
    enable_mcp_tools: bool = False,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_config_path: str | None = None,
    mcp_runner: MCPToolRunner | None = None,
    calendar_adapter: CalendarAdapter | None = None,
    embedding_coordinator_store=None,
    visual_semantic_store_pool=None,
    visual_reminder_registry=None,
    visual_memory_text_index=None,
    plugin_modules: Iterable[str] | None = None,
) -> ToolRegistry:
    """Assemble and seal the default runtime registry."""

    config = config or ProviderConfig()
    mock_mode = config.provider_mode == "mock"
    resolved_mcp_server_configs = mcp_server_configs
    if (enable_mcp_tools or not mock_mode) and resolved_mcp_server_configs is None:
        from assistant_agent.mcp.config import load_mcp_server_configs_from_env

        resolved_mcp_server_configs = load_mcp_server_configs_from_env(
            config_path=mcp_config_path
        )
    if mock_mode:
        resolved_mcp_server_configs = []
    plugin_context = ToolPluginContext(
        config=config,
        mcp_server_configs=resolved_mcp_server_configs or [],
        mcp_runner=mcp_runner,
        video_context_store=video_context_store,
        realtime_video_memory_store=realtime_video_memory_store,
        durable_task_service=durable_task_service,
        workflow_service=workflow_service,
        calendar_adapter=calendar_adapter,
        embedding_coordinator_store=embedding_coordinator_store,
        visual_semantic_store_pool=visual_semantic_store_pool,
        visual_reminder_registry=visual_reminder_registry,
        visual_memory_text_index=visual_memory_text_index,
    )
    module_names = (
        configured_plugin_modules_from_env()
        if plugin_modules is None
        else normalize_configured_plugin_modules(plugin_modules)
    )
    assembly = assemble_tool_plugins(
        plugin_context,
        builtin_plugins=default_tool_plugins(),
        configured_module_names=module_names,
    )
    contributions = list(assembly.contributions)
    sources = list(assembly.report.sources)
    issues = list(assembly.report.issues)
    if not mock_mode and (
        enable_mcp_tools
        or mcp_server_configs is not None
        or resolved_mcp_server_configs
    ):
        from assistant_agent.mcp.registration import discover_configured_mcp_tools

        server_configs = resolved_mcp_server_configs or []
        discovered, mcp_summary = discover_configured_mcp_tools(
            server_configs,
            runner=mcp_runner,
        )
        suppressed_adapter_tools = {
            namespaced_mcp_tool_name(server.adapter_config(), remote_name)
            for server in server_configs
            for remote_name in (
                server.personal_assistant_tools.weather_lookup,
                *server.email_tools.mapped_tool_names(),
            )
            if remote_name
        }
        suppressed_provider_native_tools = {
            namespaced_mcp_tool_name(server.adapter_config(), remote_name)
            for server in server_configs
            for remote_name in _PROVIDER_NATIVE_WEATHER_REMOTE_TOOL_NAMES
        }
        for server in server_configs:
            sources.append(
                ToolPluginSourceRecord(
                    source_type="mcp",
                    source_ref=server.server_name,
                    trusted=True,
                )
            )
        for item in discovered:
            if item.tool.name in (
                suppressed_adapter_tools | suppressed_provider_native_tools
            ):
                continue
            contributions.append(
                ToolContribution(
                    tool=item.tool,
                    registration=ToolRegistrationRecord(
                        tool_name=item.tool.name,
                        plugin_id=f"mcp.{item.server_name}",
                        plugin_version="1",
                        source_type="mcp",
                        source_ref=item.server_name,
                    ),
                )
            )
        issues.extend(
            ToolPluginLoadIssue(
                code="mcp_discovery_issue",
                message=message,
                source_ref="mcp",
            )
            for message in mcp_summary.issues
        )
    registry = ToolRegistry()
    try:
        registry.register_many(contributions)
    except Exception as exc:
        raise ToolPluginAssemblyError(
            ToolPluginAssemblyReport(
                sources=sources,
                issues=[
                    *issues,
                    ToolPluginLoadIssue(
                        code="atomic_registration_failed",
                        message=sanitize_error_message(exc),
                        source_ref="tool_registry",
                    ),
                ],
            )
        ) from exc
    report = ToolPluginAssemblyReport(
        sources=sources,
        registrations=[item.registration for item in contributions],
        issues=issues,
    )
    registry.seal(assembly_report=report)
    return registry


def create_realtime_video_observation_registry(
    config: ProviderConfig | None = None,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> ToolRegistry:
    """Create the governed realtime observer registry with one visual tool."""

    config = config or ProviderConfig()
    registry = ToolRegistry()
    tool = build_realtime_video_observation_tool(
        config,
        realtime_video_memory_store=realtime_video_memory_store,
    )
    registry.register(
        tool,
        ToolRegistrationRecord(
            tool_name=tool.name,
            plugin_id="vision.realtime_observer",
            plugin_version="1",
            source_type="realtime_observer",
            source_ref=(
                "assistant_agent.tools.plugins.builtin."
                "media_inspection.plugin"
            ),
        ),
    )
    registry.seal()
    return registry
