"""Select prompt-safe skill-style capability descriptors for assistant context."""

from __future__ import annotations

from assistant_agent.schemas.context import (
    ToolCapabilityCatalogSelection,
    ToolCapabilityDescriptor,
    ToolCatalogSummary,
)
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec

_TOOL_EXECUTOR_CONSTRAINT = "Selection context only; execute governed tools only through ToolExecutor."

_DEFAULT_CAPABILITIES: tuple[ToolCapabilityDescriptor, ...] = (
    ToolCapabilityDescriptor(
        name="realtime_web_search",
        description="Look up current or web-backed information during a realtime call.",
        governed_tools=["web_search"],
        required_inputs_by_tool={"web_search": ["query"]},
        when_to_use=[
            "User asks for latest, current, today, news, or web-backed information.",
        ],
        when_not_to_use=[
            "User asks for stored personal memory; use memory tools instead.",
            "User asks to buy or compare products; use product tools instead.",
        ],
        safe_examples=[
            "latest AI industry news",
            "check today's market headlines",
        ],
        runtime_constraints=[
            _TOOL_EXECUTOR_CONSTRAINT,
            "Read-only external lookup; do not execute raw HTTP from this descriptor.",
        ],
    ),
)


def select_tool_capability_descriptors(
    *,
    request: UserRequest,
    available_tool_specs: list[ToolSpec],
    prompt_tool_specs: list[ToolSpec],
    tool_catalog_summary: ToolCatalogSummary,
) -> ToolCapabilityCatalogSelection:
    """Select capability descriptors only when their governed tools are prompt-selected."""

    if not available_tool_specs:
        return ToolCapabilityCatalogSelection(
            selection_reasons=["capability_catalog_skipped: no_tools_available"],
        )
    if tool_catalog_summary.fallback_used:
        return ToolCapabilityCatalogSelection(
            selection_reasons=["capability_catalog_skipped: tool_catalog_fallback"],
            fallback_used=True,
        )

    request_text = request.text or ""
    available_names = {spec.name for spec in available_tool_specs}
    prompt_names = {spec.name for spec in prompt_tool_specs}
    capabilities: list[ToolCapabilityDescriptor] = []
    reasons: list[str] = []

    for descriptor in _DEFAULT_CAPABILITIES:
        governed_names = set(descriptor.governed_tools)
        if not governed_names.issubset(available_names):
            continue
        if not governed_names.intersection(prompt_names):
            continue
        capabilities.append(descriptor)
        reasons.append(f"capability_catalog_selected:{descriptor.name}")

    if not capabilities and request_text.strip():
        reasons.append("capability_catalog_skipped: no_matching_governed_tools")

    return ToolCapabilityCatalogSelection(
        capabilities=capabilities,
        selection_reasons=reasons,
    )
