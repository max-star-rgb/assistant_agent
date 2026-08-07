"""Deterministic per-run ToolSpec selection for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.context.models import ToolCatalogSummary
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

    explicit_skills: set[str]
    allowed_tools: set[str]
    profile: str | None


def select_prompt_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    skill_catalog: SkillCatalog | None = None,
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
    available_specs = list(qualification.qualified_tool_specs)
    selection_mode = "qualified_tools"
    if isinstance(request.metadata.get("_trusted_workflow_assignment"), dict):
        allowed = set(
            _string_list(request.metadata.get("_trusted_workflow_allowed_tools"))
        )
        available_specs = [spec for spec in available_specs if spec.name in allowed]
        selection_mode = "workflow_work_item_tools"
    elif request.metadata.get("_trusted_durable_execution") is True:
        ready = set(_string_list(request.metadata.get("ready_tool_names")))
        allowed = ready | set(DURABLE_TASK_SUBMISSION_TOOL_NAMES)
        available_specs = [
            spec for spec in available_specs if spec.name in allowed
        ]
        selection_mode = "durable_ready_tools"
    registered_names = [spec.name for spec in tool_specs]
    available_names = [spec.name for spec in available_specs]
    visibility_overrides = _visibility_overrides(request)
    active_skill_descriptors = _active_skills(
        catalog,
        visibility_overrides.explicit_skills,
        available_tool_names=set(available_names),
    )
    active_skill_ids = [
        descriptor.name for descriptor in active_skill_descriptors
    ]
    entry_profile = visibility_overrides.profile
    reasons = [
        *(f"skill_activated:{skill_id}" for skill_id in active_skill_ids),
        *([f"entry_profile:{entry_profile}"] if entry_profile else []),
        selection_mode,
    ]
    excluded_reasons = {
        name: list(items)
        for name, items in qualification.excluded_reasons.items()
    }
    if isinstance(request.metadata.get("_trusted_workflow_assignment"), dict):
        available_set = {spec.name for spec in available_specs}
        for spec in qualification.qualified_tool_specs:
            if spec.name not in available_set:
                excluded_reasons.setdefault(spec.name, []).append(
                    "workflow_work_item_not_allowed"
                )
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
    active_skill_descriptors = _active_skills(
        catalog or SkillCatalog(),
        visibility_overrides.explicit_skills,
        available_tool_names={spec.name for spec in qualified_specs},
    )
    active_skill_ids = [descriptor.name for descriptor in active_skill_descriptors]
    return ToolQualificationSelection(
        qualified_tool_specs=qualified_specs,
        active_skill_ids=active_skill_ids,
        active_skill_descriptors=active_skill_descriptors,
        excluded_reasons=excluded_reasons,
    )


def _visibility_overrides(request: UserRequest) -> ToolVisibilityOverrides:
    payload = request.metadata.get("tool_visibility")
    payload = payload if isinstance(payload, dict) else {}
    return ToolVisibilityOverrides(
        explicit_skills=set(_string_list(payload.get("enabled_skills"))),
        allowed_tools=set(_string_list(payload.get("allowed_tools"))),
        profile=_string_value(payload.get("profile")),
    )


def _active_skills(
    catalog: SkillCatalog,
    explicit_skill_ids: set[str],
    *,
    available_tool_names: set[str],
) -> list[SkillDescriptor]:
    if LOAD_SKILL_TOOL_NAME not in available_tool_names:
        return []
    active_skills: list[SkillDescriptor] = []
    for descriptor in catalog.descriptors:
        if not descriptor.enabled or descriptor.disable_model_invocation:
            continue
        permitted_tools = {
            tool_name
            for tool_name in descriptor.governed_tools
            if f"tool:{tool_name}" in descriptor.permissions
        }
        if not permitted_tools.intersection(available_tool_names):
            continue
        explicitly_enabled = descriptor.name in explicit_skill_ids
        enabled_by_default = (
            descriptor.visibility.enabled_by_default
            and not descriptor.visibility.skill_only
        )
        if explicitly_enabled or enabled_by_default:
            active_skills.append(descriptor)
    return active_skills


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
