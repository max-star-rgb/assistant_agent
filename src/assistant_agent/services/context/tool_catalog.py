"""Deterministic per-run ToolSpec selection for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_agent.schemas.context import ToolCatalogSummary
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import RunToolCatalog, ToolSpec
from assistant_agent.schemas.tool_ids import DURABLE_TASK_SUBMISSION_TOOL_NAMES
from assistant_agent.services.context.skill_loader import (
    SkillCatalog,
    load_repo_skill_descriptors,
)
from assistant_agent.services.context.tool_exposure import (
    ToolExposureCategory,
    evaluate_tool_exposure,
    tool_exposure_category,
)
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class ToolCatalogSelection:
    """Run-scoped tool assembly plus prompt-facing specs and trace summary."""

    available_tool_specs: list[ToolSpec]
    run_tool_catalog: RunToolCatalog
    summary: ToolCatalogSummary
    active_skill_ids: list[str]


@dataclass(frozen=True)
class ToolQualificationSelection:
    """Deterministic qualification result based only on structured policy facts."""

    qualified_tool_specs: list[ToolSpec]
    active_skill_ids: list[str]
    excluded_reasons: dict[str, list[str]]


@dataclass(frozen=True)
class ToolVisibilityOverrides:
    """Structured per-run tool exposure overrides."""

    explicit_tools: set[str]
    explicit_skills: set[str]
    configured_tools: set[str]
    allowed_tools: set[str]
    profile: str | None


def select_prompt_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    skill_catalog: SkillCatalog | None = None,
    registry_generation: str | None = None,
    host_configured_tool_names: set[str] | None = None,
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
        host_configured_tool_names=host_configured_tool_names,
    )
    available_specs = list(qualification.qualified_tool_specs)
    selection_mode = "qualified_tools"
    if request.metadata.get("_trusted_durable_execution") is True:
        ready = set(_string_list(request.metadata.get("ready_tool_names")))
        allowed = ready | set(DURABLE_TASK_SUBMISSION_TOOL_NAMES)
        available_specs = [
            spec for spec in available_specs if spec.name in allowed
        ]
        selection_mode = "durable_ready_tools"
    registered_names = [spec.name for spec in tool_specs]
    available_names = [spec.name for spec in available_specs]
    entry_profile = _visibility_overrides(request).profile
    reasons = [
        *(f"explicit_skill_activated:{skill_id}" for skill_id in qualification.active_skill_ids),
        *([f"entry_profile:{entry_profile}"] if entry_profile else []),
        selection_mode,
    ]
    excluded_reasons = {
        name: list(items)
        for name, items in qualification.excluded_reasons.items()
    }
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
        active_skill_ids=qualification.active_skill_ids,
    )


def qualify_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    catalog: SkillCatalog | None = None,
    host_configured_tool_names: set[str] | None = None,
) -> ToolQualificationSelection:
    """Return tools allowed by environment, visibility policy, and exposure class."""

    visibility_overrides = _visibility_overrides(request)
    host_configured = host_configured_tool_names or set()
    active_skill_ids, active_skill_tools = _explicit_skill_activation(
        catalog or SkillCatalog(),
        visibility_overrides.explicit_skills,
    )
    qualified_specs: list[ToolSpec] = []
    excluded_reasons: dict[str, list[str]] = {}
    trusted_durable_execution = request.metadata.get("_trusted_durable_execution") is True
    durable_ready_tool_names = set(
        _string_list(request.metadata.get("ready_tool_names"))
    ) | set(DURABLE_TASK_SUBMISSION_TOOL_NAMES)
    for spec in tool_specs:
        if visibility_overrides.allowed_tools and spec.name not in visibility_overrides.allowed_tools:
            excluded_reasons[spec.name] = ["entry_profile_not_allowed"]
            continue
        category = tool_exposure_category(spec)
        durable_ready = trusted_durable_execution and spec.name in durable_ready_tool_names
        durable_plan_submission = (
            request.task_execution_mode == "durable"
            and spec.name in DURABLE_TASK_SUBMISSION_TOOL_NAMES
        )
        configured_for_exposure = (
            _code_configured_tool_exposure(
                category=category,
                tool_name=spec.name,
                host_configured_tool_names=host_configured,
            )
            or durable_ready
            or durable_plan_submission
            or spec.name in visibility_overrides.configured_tools
        )
        explicitly_enabled = (
            spec.name in visibility_overrides.explicit_tools
            or spec.name in active_skill_tools
        )
        if not spec.enabled_by_default and not (
            configured_for_exposure or explicitly_enabled
        ):
            excluded_reasons[spec.name] = ["disabled_by_default"]
            continue
        exposure = evaluate_tool_exposure(
            request,
            spec,
            configured_for_exposure=configured_for_exposure,
            explicitly_enabled=explicitly_enabled,
        )
        if not exposure.exposed:
            excluded_reasons[spec.name] = list(exposure.excluded_reasons)
            if not excluded_reasons[spec.name]:
                excluded_reasons[spec.name] = ["tool_not_exposed"]
            continue
        qualified_specs.append(spec)
    return ToolQualificationSelection(
        qualified_tool_specs=qualified_specs,
        active_skill_ids=active_skill_ids,
        excluded_reasons=excluded_reasons,
    )


def _visibility_overrides(request: UserRequest) -> ToolVisibilityOverrides:
    payload = request.metadata.get("tool_visibility")
    payload = payload if isinstance(payload, dict) else {}
    return ToolVisibilityOverrides(
        explicit_tools=set(_string_list(payload.get("enabled_tools"))),
        explicit_skills=set(_string_list(payload.get("enabled_skills"))),
        configured_tools=set(_string_list(payload.get("configured_tools"))),
        allowed_tools=set(_string_list(payload.get("allowed_tools"))),
        profile=_string_value(payload.get("profile")),
    )


def _code_configured_tool_exposure(
    *,
    category: ToolExposureCategory,
    tool_name: str,
    host_configured_tool_names: set[str],
) -> bool:
    if category == "generate":
        return True
    if category == "write" and tool_name in host_configured_tool_names:
        return True
    return False


def _explicit_skill_activation(
    catalog: SkillCatalog,
    explicit_skill_ids: set[str],
) -> tuple[list[str], set[str]]:
    active_skill_ids: list[str] = []
    active_tools: set[str] = set()
    for descriptor in catalog.descriptors:
        if descriptor.name not in explicit_skill_ids:
            continue
        if not descriptor.enabled or descriptor.disable_model_invocation:
            continue
        permitted_tools = {
            tool_name
            for tool_name in descriptor.governed_tools
            if f"tool:{tool_name}" in descriptor.permissions
        }
        if not permitted_tools:
            continue
        active_skill_ids.append(descriptor.name)
        active_tools.update(permitted_tools)
    return active_skill_ids, active_tools


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
