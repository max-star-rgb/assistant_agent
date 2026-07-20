"""Select prompt-safe skill-style capability descriptors for assistant context."""

from __future__ import annotations

from pathlib import Path

from assistant_agent.schemas.context import (
    SkillExposureReport,
    SkillExposureSkip,
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
from assistant_agent.services.context.skill_recall import recall_skill_descriptors

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
            "ToolExecutor may retry retryable transient failures once.",
        ],
    ),
)


def select_tool_capability_descriptors(
    *,
    request: UserRequest,
    qualified_tool_specs: list[ToolSpec],
    prompt_tool_specs: list[ToolSpec],
    tool_catalog_summary: ToolCatalogSummary,
    active_skill_ids: set[str] | None = None,
    repo_root: Path | None = None,
    skill_catalog: SkillCatalog | None = None,
) -> ToolCapabilityCatalogSelection:
    """Expose capability descriptors for explicit and auto-recalled skills."""

    if not qualified_tool_specs:
        return ToolCapabilityCatalogSelection(
            selection_reasons=["capability_catalog_skipped: no_tools_qualified"],
            skill_report=SkillExposureReport(),
        )
    if tool_catalog_summary.fallback_used:
        return ToolCapabilityCatalogSelection(
            selection_reasons=["capability_catalog_skipped: tool_catalog_fallback"],
            fallback_used=True,
            skill_report=SkillExposureReport(),
        )

    explicit_ids = (
        _explicit_skill_ids(request)
        if active_skill_ids is None
        else active_skill_ids
    )
    qualified_names = {spec.name for spec in qualified_tool_specs}
    prompt_names = {spec.name for spec in prompt_tool_specs}
    capabilities: list[ToolCapabilityDescriptor] = []
    catalog_descriptors, reasons, report = _candidate_capability_descriptors(
        repo_root=repo_root,
        skill_catalog=skill_catalog,
    )
    recall = recall_skill_descriptors(request, catalog_descriptors)
    auto_ids = set(recall.candidate_skill_ids)
    active_ids = set(explicit_ids) | auto_ids
    report.explicit_skill_ids = _unique(sorted(explicit_ids))
    report.auto_candidate_skill_ids = list(recall.candidate_skill_ids)
    report.auto_recall_reasons = dict(recall.reasons_by_skill)

    for descriptor in catalog_descriptors:
        if descriptor.name in auto_ids:
            reasons.append(f"capability_catalog_auto_recalled:{descriptor.name}")
        if descriptor.name not in active_ids:
            report.skipped.append(
                SkillExposureSkip(
                    skill_id=descriptor.name,
                    reason="skill_not_explicitly_enabled",
                )
            )
            reasons.append(
                f"capability_catalog_skipped:{descriptor.name}:skill_not_explicitly_enabled"
            )
            continue
        governed_names = set(descriptor.governed_tools)
        if not governed_names.issubset(qualified_names):
            missing_names = sorted(governed_names - qualified_names)
            report.unavailable_tool_count += len(missing_names)
            for tool_name in missing_names:
                report.skipped.append(
                    SkillExposureSkip(
                        skill_id=descriptor.name,
                        reason="governed_tool_unqualified",
                        tool_name=tool_name,
                    )
                )
            reasons.append(
                f"capability_catalog_skipped:{descriptor.name}:governed_tool_unqualified"
            )
            continue
        if not governed_names.intersection(prompt_names):
            report.skipped.append(
                SkillExposureSkip(
                    skill_id=descriptor.name,
                    reason="governed_tool_not_prompt_selected",
                )
            )
            reasons.append(
                f"capability_catalog_skipped:{descriptor.name}:governed_tool_not_prompt_selected"
            )
            continue
        capabilities.append(descriptor)
        report.selected_skill_ids.append(descriptor.name)
        report.governed_tool_names = _unique(
            report.governed_tool_names + descriptor.governed_tools
        )
        reasons.append(f"capability_catalog_selected:{descriptor.name}")

    if not capabilities:
        if active_ids:
            reasons.append("capability_catalog_skipped: no_selected_qualified_skills")
        else:
            reasons.append(
                "capability_catalog_skipped: no_explicitly_active_qualified_skills"
            )

    return ToolCapabilityCatalogSelection(
        capabilities=capabilities,
        selection_reasons=reasons,
        skill_report=report,
    )


def _explicit_skill_ids(request: UserRequest) -> set[str]:
    payload = request.metadata.get("tool_visibility")
    if not isinstance(payload, dict):
        return set()
    value = payload.get("enabled_skills")
    if not isinstance(value, list):
        return set()
    return {
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    }


def _candidate_capability_descriptors(
    *,
    repo_root: Path | None,
    skill_catalog: SkillCatalog | None,
) -> tuple[list[ToolCapabilityDescriptor], list[str], SkillExposureReport]:
    catalog = skill_catalog or load_repo_skill_descriptors(repo_root or _DEFAULT_REPO_ROOT)
    repo_descriptors = [
        _skill_descriptor_to_tool_capability(descriptor)
        for descriptor in catalog.descriptors
        if descriptor.enabled and not descriptor.disable_model_invocation
    ]
    repo_names = {descriptor.name for descriptor in repo_descriptors}
    issue_names = {issue.skill_id for issue in catalog.issues if issue.skill_id}
    override_names = _unique(sorted(repo_names | issue_names))
    descriptors = list(repo_descriptors)
    builtin_fallbacks = [
        descriptor for descriptor in _DEFAULT_CAPABILITIES if descriptor.name not in override_names
    ]
    descriptors.extend(builtin_fallbacks)

    reasons = [
        f"capability_catalog_repo_skill_loaded:{descriptor.name}"
        for descriptor in repo_descriptors
    ]
    reasons.extend(
        f"capability_catalog_skill_issue:{issue.skill_id or 'unknown'}:{issue.code}"
        for issue in catalog.issues
    )
    report = SkillExposureReport(
        loaded_skill_ids=_unique(sorted(repo_names)),
        builtin_fallback_skill_ids=[descriptor.name for descriptor in builtin_fallbacks],
        override_skill_ids=override_names,
        skipped=[
            SkillExposureSkip(skill_id=issue.skill_id or "unknown", reason=issue.code)
            for issue in catalog.issues
        ],
        permission_issue_count=sum(
            1
            for issue in catalog.issues
            if issue.code in {"missing_tool_permission", "invalid_permission"}
        ),
    )
    return descriptors, reasons, report


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


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
