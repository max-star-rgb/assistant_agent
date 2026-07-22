"""Operator-visible startup summary for the finalized Tool Registry."""

from assistant_agent.tools.registry import ToolRegistry


def format_tool_registry_summary(registry: ToolRegistry) -> list[str]:
    """Describe the exact sealed Registry used by the running server."""

    registrations = registry.list_registration_records()
    lines = [f"Registered tools ({len(registrations)}):"]
    if not registrations:
        lines.append("  none")
    else:
        for registration in registrations:
            owner = f"{registration.plugin_id}@{registration.plugin_version}"
            lines.append(
                f"  {registration.tool_name} "
                f"(plugin={owner}, source={registration.source_type})"
            )
    lines.append(
        f"Tool registry: sealed={registry.sealed}, generation={registry.generation}"
    )
    return lines


def print_tool_registry_summary(registry: ToolRegistry) -> None:
    for line in format_tool_registry_summary(registry):
        print(line)
