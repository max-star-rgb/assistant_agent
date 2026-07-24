"""Operator-visible startup summary for the finalized Tool Registry."""

from collections import defaultdict

from assistant_agent.tools.registry import ToolRegistry


def format_tool_registry_summary(registry: ToolRegistry) -> list[str]:
    """List registered tool names grouped by their owning plugin."""

    grouped_names: dict[str, list[str]] = defaultdict(list)
    for record in registry.list_registration_records():
        grouped_names[record.plugin_id].append(record.tool_name)

    lines: list[str] = []
    for plugin_id in sorted(grouped_names):
        lines.append(f"[{plugin_id}]")
        lines.extend(f"  {name}" for name in sorted(grouped_names[plugin_id]))
    return lines


def print_tool_registry_summary(registry: ToolRegistry) -> None:
    for line in format_tool_registry_summary(registry):
        print(line)
