"""Compatibility exports for the relocated Tool Plugin contract API."""

from assistant_agent.tools.plugins.contracts import (
    LoadedToolPlugin,
    ToolPlugin,
    ToolPluginAssemblyReport,
    ToolPluginContext,
    ToolPluginDescriptor,
    ToolPluginLoadIssue,
    ToolPluginSourceRecord,
    ToolRegistrationRecord,
)

__all__ = [
    "LoadedToolPlugin",
    "ToolPlugin",
    "ToolPluginAssemblyReport",
    "ToolPluginContext",
    "ToolPluginDescriptor",
    "ToolPluginLoadIssue",
    "ToolPluginSourceRecord",
    "ToolRegistrationRecord",
]
