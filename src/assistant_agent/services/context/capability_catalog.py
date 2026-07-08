"""Select prompt-safe skill-style capability descriptors for assistant context."""

from __future__ import annotations

from pathlib import Path

from assistant_agent.schemas.context import (
    ToolCapabilityCatalogSelection,
    ToolCapabilityDescriptor,
    ToolCatalogSummary,
)
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.context.skill_loader import (
    SkillCatalog,
    SkillDescriptor,
    load_repo_skill_descriptors,
)

_TOOL_EXECUTOR_CONSTRAINT = "Selection context only; execute governed tools only through ToolExecutor."
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]

_DEFAULT_CAPABILITIES: tuple[ToolCapabilityDescriptor, ...] = (
    ToolCapabilityDescriptor(
        name="realtime_web_search",
        description="Look up current or web-backed information during a realtime call.",
        governed_tools=["web_search"],
        permissions=["tool:web_search"],
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
    repo_root: Path | None = None,
    skill_catalog: SkillCatalog | None = None,
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
    catalog_descriptors, reasons = _candidate_capability_descriptors(
        repo_root=repo_root,
        skill_catalog=skill_catalog,
    )

    for descriptor in catalog_descriptors:
        governed_names = set(descriptor.governed_tools)
        if not governed_names.issubset(available_names):
            reasons.append(
                f"capability_catalog_skipped:{descriptor.name}:governed_tool_unavailable"
            )
            continue
        if not governed_names.intersection(prompt_names):
            reasons.append(
                f"capability_catalog_skipped:{descriptor.name}:governed_tool_not_prompt_selected"
            )
            continue
        capabilities.append(descriptor)
        reasons.append(f"capability_catalog_selected:{descriptor.name}")

    if not capabilities and request_text.strip():
        reasons.append("capability_catalog_skipped: no_matching_governed_tools")

    return ToolCapabilityCatalogSelection(
        capabilities=capabilities,
        selection_reasons=reasons,
    )


def _candidate_capability_descriptors(
    *,
    repo_root: Path | None,
    skill_catalog: SkillCatalog | None,
) -> tuple[list[ToolCapabilityDescriptor], list[str]]:
    catalog = skill_catalog or load_repo_skill_descriptors(repo_root or _DEFAULT_REPO_ROOT)
    repo_descriptors = [
        _skill_descriptor_to_tool_capability(descriptor)
        for descriptor in catalog.descriptors
        if descriptor.enabled and not descriptor.disable_model_invocation
    ]
    repo_names = {descriptor.name for descriptor in repo_descriptors}
    descriptors = list(repo_descriptors)
    descriptors.extend(
        descriptor for descriptor in _DEFAULT_CAPABILITIES if descriptor.name not in repo_names
    )

    reasons = [
        f"capability_catalog_repo_skill_loaded:{descriptor.name}"
        for descriptor in repo_descriptors
    ]
    reasons.extend(
        f"capability_catalog_skill_issue:{issue.skill_id or 'unknown'}:{issue.code}"
        for issue in catalog.issues
    )
    return descriptors, reasons


def _skill_descriptor_to_tool_capability(
    descriptor: SkillDescriptor,
) -> ToolCapabilityDescriptor:
    runtime_constraints = list(descriptor.runtime_constraints)
    if _TOOL_EXECUTOR_CONSTRAINT not in runtime_constraints:
        runtime_constraints.insert(0, _TOOL_EXECUTOR_CONSTRAINT)
    return ToolCapabilityDescriptor(
        name=descriptor.name,
        description=descriptor.description,
        governed_tools=descriptor.governed_tools,
        permissions=descriptor.permissions,
        required_inputs_by_tool=descriptor.required_inputs_by_tool,
        when_to_use=descriptor.when_to_use,
        when_not_to_use=descriptor.when_not_to_use,
        safe_examples=descriptor.safe_examples,
        runtime_constraints=runtime_constraints,
    )
