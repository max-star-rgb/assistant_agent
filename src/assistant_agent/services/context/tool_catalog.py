"""Deterministic prompt ToolSpec recall for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from assistant_agent.schemas.context import ToolCatalogSummary
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import RunToolCatalog, ToolSpec
from assistant_agent.services.context.skill_loader import (
    SkillCatalog,
    load_repo_skill_descriptors,
)
from assistant_agent.services.context.tool_exposure import (
    ToolExposureCategory,
    entry_profile_tool_exposure,
    tool_exposure_category,
)
from assistant_agent.services.tool_manifest import MEMORY_MEDIA_INGEST_TOOL_NAME, MEMORY_SAVE_TOOL_NAME


_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
_CODE_CONFIGURED_WRITE_TOOL_NAMES = {MEMORY_SAVE_TOOL_NAME, MEMORY_MEDIA_INGEST_TOOL_NAME}


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
    explicit_toolsets: set[str]
    explicit_skills: set[str]
    configured_tools: set[str]
    configured_toolsets: set[str]


def select_prompt_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    skill_catalog: SkillCatalog | None = None,
) -> ToolCatalogSelection:
    """Qualify registered tools, then expose all qualified tools via identity recall."""

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
    available_specs = recall_qualified_tool_specs(
        request,
        qualification.qualified_tool_specs,
    )
    registered_names = [spec.name for spec in tool_specs]
    available_names = [spec.name for spec in available_specs]
    reasons = [
        *(f"explicit_skill_activated:{skill_id}" for skill_id in qualification.active_skill_ids),
        "recall_identity",
    ]
    run_tool_catalog = RunToolCatalog(
        available_tool_names=available_names,
        selection_reasons=reasons,
        excluded_reasons=qualification.excluded_reasons,
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
        ),
        active_skill_ids=qualification.active_skill_ids,
    )


def qualify_tool_specs(
    request: UserRequest,
    tool_specs: list[ToolSpec],
    *,
    catalog: SkillCatalog | None = None,
) -> ToolQualificationSelection:
    """Return tools allowed by environment, visibility policy, and exposure class."""

    visibility_overrides = _visibility_overrides(request)
    active_skill_ids, active_skill_tools = _explicit_skill_activation(
        catalog or SkillCatalog(),
        visibility_overrides.explicit_skills,
    )
    qualified_specs: list[ToolSpec] = []
    excluded_reasons: dict[str, list[str]] = {}
    trusted_durable_execution = request.metadata.get("_trusted_durable_execution") is True
    durable_ready_tool_names = set(_string_list(request.metadata.get("ready_tool_names"))) | {
        "task_plan_submit"
    }
    for spec in tool_specs:
        category = tool_exposure_category(spec)
        durable_ready = trusted_durable_execution and spec.name in durable_ready_tool_names
        durable_plan_submission = (
            request.task_execution_mode == "durable" and spec.name == "task_plan_submit"
        )
        configured_for_exposure = (
            _code_configured_tool_exposure(category=category, tool_name=spec.name)
            or durable_ready
            or durable_plan_submission
            or spec.name in visibility_overrides.configured_tools
            or bool(
                spec.toolset
                and spec.toolset in visibility_overrides.configured_toolsets
            )
        )
        explicitly_enabled = (
            spec.name in visibility_overrides.explicit_tools
            or bool(
                spec.toolset
                and spec.toolset in visibility_overrides.explicit_toolsets
            )
            or spec.name in active_skill_tools
        )
        missing_env = [name for name in spec.requires_env if not os.environ.get(name)]
        if missing_env:
            excluded_reasons[spec.name] = [
                f"missing_required_env:{name}" for name in missing_env
            ]
            continue
        if spec.skill_only and spec.name not in active_skill_tools:
            excluded_reasons[spec.name] = ["skill_activation_required"]
            continue
        if not spec.enabled_by_default and not (
            configured_for_exposure or explicitly_enabled
        ):
            excluded_reasons[spec.name] = ["disabled_by_default"]
            continue
        exposure = entry_profile_tool_exposure(
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


def recall_qualified_tool_specs(
    request: UserRequest,
    qualified_tool_specs: list[ToolSpec],
) -> list[ToolSpec]:
    """Return every qualified ToolSpec until a future recall design is approved."""

    if request.metadata.get("_trusted_durable_execution") is True:
        ready = set(_string_list(request.metadata.get("ready_tool_names")))
        allowed = ready | {"task_plan_submit"}
        return [spec for spec in qualified_tool_specs if spec.name in allowed]
    return list(qualified_tool_specs)


def _visibility_overrides(request: UserRequest) -> ToolVisibilityOverrides:
    payload = request.metadata.get("tool_visibility")
    payload = payload if isinstance(payload, dict) else {}
    return ToolVisibilityOverrides(
        explicit_tools=set(_string_list(payload.get("enabled_tools"))),
        explicit_toolsets=set(_string_list(payload.get("enabled_toolsets"))),
        explicit_skills=set(_string_list(payload.get("enabled_skills"))),
        configured_tools=set(_string_list(payload.get("configured_tools"))),
        configured_toolsets=set(_string_list(payload.get("configured_toolsets"))),
    )


def _code_configured_tool_exposure(
    *,
    category: ToolExposureCategory,
    tool_name: str,
) -> bool:
    if category == "generate":
        return True
    if category == "write" and tool_name in _CODE_CONFIGURED_WRITE_TOOL_NAMES:
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


def prompt_tool_spec_payload(spec: ToolSpec) -> dict[str, Any]:
    """Return the compact legacy prompt-json payload for one ToolSpec."""

    payload = spec.model_dump(
        mode="json",
        include={
            "name",
            "description",
            "input_schema",
            "required_inputs",
            "when_to_use",
            "when_not_to_use",
            "runtime_constraints",
        },
    )
    input_schema = payload.get("input_schema")
    if isinstance(input_schema, dict):
        payload["input_schema"] = _compact_prompt_input_schema(input_schema)
    return payload


def _compact_prompt_input_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    fields = input_schema.get("fields")
    if not isinstance(fields, dict):
        return input_schema
    compact_fields: dict[str, Any] = {}
    for field_name, field_info in fields.items():
        if not isinstance(field_name, str) or not isinstance(field_info, dict):
            continue
        compact_field = {
            key: value
            for key, value in field_info.items()
            if key in {"type", "required"} and value is not None
        }
        description = field_info.get("description")
        if isinstance(description, str) and description.strip():
            compact_field["description"] = _clip_prompt_description(description)
        compact_fields[field_name] = compact_field
    return {**input_schema, "fields": compact_fields}


def _clip_prompt_description(description: str, *, max_chars: int = 80) -> str:
    text = description.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + "...[trimmed]"
