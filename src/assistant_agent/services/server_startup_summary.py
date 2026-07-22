"""Operator-visible startup summary for the finalized Tool Registry."""

from collections import defaultdict

from assistant_agent.tools.registry import ToolRegistry

_NORMAL_TOOLSET_LABEL = "normal（通用）"
_PERSONAL_TOOLSET_LABEL = "personal"


def format_tool_registry_summary(registry: ToolRegistry) -> list[str]:
    """List registered tool names grouped by ToolSpec toolset."""

    grouped_names: dict[str, list[str]] = defaultdict(list)
    for spec in registry.list_specs():
        grouped_names[_summary_toolset(spec.toolset)].append(spec.name)

    lines: list[str] = []
    for toolset in sorted(
        grouped_names,
        key=lambda item: (item != _NORMAL_TOOLSET_LABEL, item),
    ):
        lines.append(f"[{toolset}]")
        lines.extend(f"  {name}" for name in sorted(grouped_names[toolset]))
    return lines


def _summary_toolset(toolset: str | None) -> str:
    if not toolset:
        return _NORMAL_TOOLSET_LABEL
    if toolset == _PERSONAL_TOOLSET_LABEL or toolset.startswith("personal."):
        return _PERSONAL_TOOLSET_LABEL
    return toolset


def print_tool_registry_summary(registry: ToolRegistry) -> None:
    for line in format_tool_registry_summary(registry):
        print(line)
