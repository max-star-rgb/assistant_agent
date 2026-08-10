"""Deterministic per-run ToolSpec selection for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.context.models import ToolCatalogSummary
from assistant_agent.runtime.capability_grants import CapabilityGrant
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import RunToolCatalog, ToolSpec
from assistant_agent.tools.ids import (
    DURABLE_TASK_SUBMISSION_TOOL_NAMES,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.skills.loading import (
    SkillCatalog,
    SkillDescriptor,
    default_repo_root,
    load_repo_skill_descriptors,
)
from assistant_agent.context.tool_exposure import (
    evaluate_tool_exposure,
)
_DEFAULT_REPO_ROOT = default_repo_root()


@dataclass(frozen=True)
class ToolCatalogSelection:
    """Run-scoped tool assembly plus prompt-facing specs and trace summary."""

    available_tool_specs: list[ToolSpec]
    run_tool_catalog: RunToolCatalog
    summary: ToolCatalogSummary
    active_skill_ids: list[str]
    active_skill_descriptors: list[SkillDescriptor]
    discoverable_skill_ids: list[str]
    discoverable_skill_descriptors: list[SkillDescriptor]
    capability_grant_ids: list[str]
    skill_granted_tool_names: list[str]
    skill_descriptors: list[SkillDescriptor]


@dataclass(frozen=True)
class ToolQualificationSelection:
    """Deterministic qualification result based only on structured policy facts."""

    qualified_tool_specs: list[ToolSpec]
    active_skill_ids: list[str]
    active_skill_descriptors: list[SkillDescriptor]
    excluded_reasons: dict[str, list[str]]


@dataclass(frozen=True)
class ToolVisibilityOverrides:
    """Structured per-run tool exposure overrides."""

    allowed_tools: set[str]
    profile: str | None


def select_prompt_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    skill_catalog: SkillCatalog | None = None,
    capability_grants: list[CapabilityGrant] | None = None,
    registry_generation: str | None = None,
) -> ToolCatalogSelection:
    """Return the complete run-qualified ToolSpec catalog."""

    catalog = (
        skill_catalog
        if skill_catalog is not None
        else load_repo_skill_descriptors(_DEFAULT_REPO_ROOT)
    )
    qualification = qualify_tool_specs(
        request,
        tool_specs,
        catalog=catalog,
    )
    eligible_specs = list(qualification.qualified_tool_specs)
    trusted_workflow = isinstance(
        request.metadata.get("_trusted_workflow_assignment"),
        dict,
    )
    trusted_durable = request.metadata.get("_trusted_durable_execution") is True
    bypass_skill_projection = trusted_workflow or trusted_durable
    effective_grants = (
        [] if bypass_skill_projection else list(capability_grants or [])
    )
    active_skill_descriptors = _active_skill_descriptors(
        catalog,
        effective_grants,
    )
    active_skill_ids = [descriptor.name for descriptor in active_skill_descriptors]
    valid_grants = _valid_capability_grants(catalog, effective_grants)
    granted_tool_names = {
        tool_name
        for grant in valid_grants
        for tool_name in grant.tool_names
    }
    enabled_descriptors = _enabled_descriptors(catalog)
    claimed_tool_names = {
        tool_name
        for descriptor in enabled_descriptors
        for tool_name in descriptor.governed_tools
    }
    available_specs = [
        spec
        for spec in eligible_specs
        if spec.name not in claimed_tool_names or spec.name in granted_tool_names
    ]
    selection_mode = "qualified_tools"
    if trusted_workflow:
        allowed = set(
            _string_list(request.metadata.get("_trusted_workflow_allowed_tools"))
        )
        available_specs = [spec for spec in eligible_specs if spec.name in allowed]
        selection_mode = "workflow_work_item_tools"
    elif trusted_durable:
        ready = set(_string_list(request.metadata.get("ready_tool_names")))
        allowed = ready | set(DURABLE_TASK_SUBMISSION_TOOL_NAMES)
        available_specs = [
            spec for spec in eligible_specs if spec.name in allowed
        ]
        selection_mode = "durable_ready_tools"
    registered_names = [spec.name for spec in tool_specs]
    available_names = [spec.name for spec in available_specs]
    discoverable_skill_descriptors = (
        []
        if bypass_skill_projection
        else _discoverable_skills(
            catalog,
            active_skill_ids=set(active_skill_ids),
            eligible_tool_names={spec.name for spec in eligible_specs},
        )
    )
    discoverable_skill_ids = [
        descriptor.name for descriptor in discoverable_skill_descriptors
    ]
    visibility_overrides = _visibility_overrides(request)
    entry_profile = visibility_overrides.profile
    reasons = [
        *(f"capability_grant:{grant.grant_id}" for grant in valid_grants),
        *(f"skill_discoverable:{skill_id}" for skill_id in discoverable_skill_ids),
        *([f"entry_profile:{entry_profile}"] if entry_profile else []),
        selection_mode,
    ]
    excluded_reasons = {
        name: list(items)
        for name, items in qualification.excluded_reasons.items()
    }
    if trusted_workflow:
        available_set = {spec.name for spec in available_specs}
        for spec in qualification.qualified_tool_specs:
            if spec.name not in available_set:
                excluded_reasons.setdefault(spec.name, []).append(
                    "workflow_work_item_not_allowed"
                )
    elif not trusted_durable:
        available_set = {spec.name for spec in available_specs}
        for spec in eligible_specs:
            if spec.name not in available_set and spec.name in claimed_tool_names:
                excluded_reasons.setdefault(spec.name, []).append(
                    "capability_not_granted"
                )
    skill_granted_tool_names = [
        spec.name
        for spec in available_specs
        if spec.name in granted_tool_names
    ]
    run_tool_catalog = RunToolCatalog(
        available_tool_names=available_names,
        selection_reasons=reasons,
        excluded_reasons=excluded_reasons,
    )
    return ToolCatalogSelection(
        available_tool_specs=available_specs,
        run_tool_catalog=run_tool_catalog,
        summary=ToolCatalogSummary(
            total_tool_count=len(registered_names),
            prompt_tool_count=len(available_specs),
            filtered_tool_count=max(len(registered_names) - len(available_specs), 0),
            selected_tool_names=available_names,
            selection_reasons=reasons,
            fallback_used=False,
            registry_generation=registry_generation,
        ),
        active_skill_ids=active_skill_ids,
        active_skill_descriptors=active_skill_descriptors,
        discoverable_skill_ids=discoverable_skill_ids,
        discoverable_skill_descriptors=discoverable_skill_descriptors,
        capability_grant_ids=[grant.grant_id for grant in valid_grants],
        skill_granted_tool_names=skill_granted_tool_names,
        skill_descriptors=list(catalog.descriptors),
    )


def qualify_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    catalog: SkillCatalog | None = None,
) -> ToolQualificationSelection:
    """Return registered tools allowed by structured run constraints."""

    visibility_overrides = _visibility_overrides(request)
    qualified_specs: list[ToolSpec] = []
    excluded_reasons: dict[str, list[str]] = {}
    for spec in tool_specs:
        if visibility_overrides.allowed_tools and spec.name not in visibility_overrides.allowed_tools:
            excluded_reasons[spec.name] = ["entry_profile_not_allowed"]
            continue
        exposure = evaluate_tool_exposure(request, spec)
        if not exposure.exposed:
            excluded_reasons[spec.name] = list(exposure.excluded_reasons)
            if not excluded_reasons[spec.name]:
                excluded_reasons[spec.name] = ["tool_not_exposed"]
            continue
        qualified_specs.append(spec)
    return ToolQualificationSelection(
        qualified_tool_specs=qualified_specs,
        active_skill_ids=[],
        active_skill_descriptors=[],
        excluded_reasons=excluded_reasons,
    )


def _visibility_overrides(request: UserRequest) -> ToolVisibilityOverrides:
    payload = request.metadata.get("tool_visibility")
    payload = payload if isinstance(payload, dict) else {}
    return ToolVisibilityOverrides(
        allowed_tools=set(_string_list(payload.get("allowed_tools"))),
        profile=_string_value(payload.get("profile")),
    )


def _enabled_descriptors(catalog: SkillCatalog) -> list[SkillDescriptor]:
    return [
        descriptor
        for descriptor in catalog.descriptors
        if descriptor.enabled
        and not (
            descriptor.activation == "model"
            and descriptor.disable_model_invocation
        )
    ]


def _valid_capability_grants(
    catalog: SkillCatalog,
    grants: list[CapabilityGrant],
) -> list[CapabilityGrant]:
    descriptors = {
        descriptor.name: descriptor
        for descriptor in _enabled_descriptors(catalog)
    }
    valid: list[CapabilityGrant] = []
    for grant in grants:
        if grant.source == "tool_search":
            continue
        descriptor = descriptors.get(grant.skill_id or "")
        if descriptor is None:
            continue
        expected_source = (
            "context" if descriptor.activation == "context" else "skill"
        )
        if grant.source != expected_source:
            continue
        valid.append(
            grant.model_copy(update={"tool_names": list(descriptor.governed_tools)})
        )
    return valid


def _active_skill_descriptors(
    catalog: SkillCatalog,
    grants: list[CapabilityGrant],
) -> list[SkillDescriptor]:
    active_ids = {
        grant.skill_id
        for grant in _valid_capability_grants(catalog, grants)
        if grant.skill_id is not None
    }
    return [
        descriptor
        for descriptor in _enabled_descriptors(catalog)
        if descriptor.name in active_ids
    ]


def _discoverable_skills(
    catalog: SkillCatalog,
    *,
    active_skill_ids: set[str],
    eligible_tool_names: set[str],
) -> list[SkillDescriptor]:
    if LOAD_SKILL_TOOL_NAME not in eligible_tool_names:
        return []
    return [
        descriptor
        for descriptor in _enabled_descriptors(catalog)
        if descriptor.activation == "model"
        and descriptor.discoverable
        and not descriptor.disable_model_invocation
        and descriptor.name not in active_skill_ids
        and bool(set(descriptor.governed_tools).intersection(eligible_tool_names))
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def prompt_tool_spec_payload(spec: ToolSpec) -> dict[str, Any]:
    """Return a prompt-compact view without changing the canonical ToolSpec."""

    payload = spec.model_dump(
        mode="json",
        include={
            "name",
            "description",
            "input_schema",
        },
    )
    input_schema = payload.get("input_schema")
    if isinstance(input_schema, dict):
        payload["input_schema"] = _compact_prompt_input_schema(input_schema)
    return payload


def _compact_prompt_input_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    return _compact_schema_value(input_schema)


def _compact_schema_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_schema_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key == "title":
            continue
        if key == "description" and isinstance(item, str):
            compact[key] = _clip_prompt_description(item)
            continue
        compact[key] = _compact_schema_value(item)
    return compact


def _clip_prompt_description(description: str, *, max_chars: int = 80) -> str:
    text = description.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + "...[trimmed]"
