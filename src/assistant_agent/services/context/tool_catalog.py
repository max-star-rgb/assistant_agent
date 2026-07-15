"""Deterministic prompt ToolSpec recall for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from assistant_agent.schemas.context import ToolCatalogSummary
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import RunToolSet, ToolSpec
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.services.context.skill_loader import (
    SkillCatalog,
    load_repo_skill_descriptors,
)
from assistant_agent.services.tool_policy import ToolPolicyInterpreter


_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_SERVICE_TOOL_NAMES = {
    "web_search",
    "product_search",
    "price_compare",
    "memory_retrieval",
    "memory_save",
}


@dataclass(frozen=True)
class ToolCatalogSelection:
    """Run-scoped tool assembly plus prompt-facing specs and trace summary."""

    qualified_tool_specs: list[ToolSpec]
    prompt_tool_specs: list[ToolSpec]
    run_tool_set: RunToolSet
    summary: ToolCatalogSummary
    active_skill_ids: list[str]


@dataclass(frozen=True)
class ToolQualificationSelection:
    """Deterministic qualification result based only on structured policy facts."""

    qualified_tool_specs: list[ToolSpec]
    active_skill_ids: list[str]
    excluded_reasons: dict[str, list[str]]


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
    prompt_specs = recall_qualified_tool_specs(
        request,
        qualification.qualified_tool_specs,
    )
    registered_names = [spec.name for spec in tool_specs]
    qualified_names = [spec.name for spec in qualification.qualified_tool_specs]
    prompt_names = [spec.name for spec in prompt_specs]
    reasons = [
        *(f"explicit_skill_activated:{skill_id}" for skill_id in qualification.active_skill_ids),
        "recall_identity",
    ]
    run_tool_set = RunToolSet(
        registered_tool_names=registered_names,
        qualified_tool_names=qualified_names,
        exposed_tool_names=prompt_names,
        executable_tool_names=prompt_names,
        selection_reasons=reasons,
        excluded_reasons=qualification.excluded_reasons,
    )
    return ToolCatalogSelection(
        qualified_tool_specs=qualification.qualified_tool_specs,
        prompt_tool_specs=prompt_specs,
        run_tool_set=run_tool_set,
        summary=ToolCatalogSummary(
            total_tool_count=len(registered_names),
            prompt_tool_count=len(prompt_specs),
            filtered_tool_count=max(len(registered_names) - len(prompt_specs), 0),
            selected_tool_names=prompt_names,
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
    """Return tools allowed by structured environment and visibility policy."""

    explicit_tools, explicit_toolsets, explicit_skills = _visibility_overrides(request)
    active_skill_ids, active_skill_tools = _explicit_skill_activation(
        catalog or SkillCatalog(),
        explicit_skills,
    )
    qualified_specs: list[ToolSpec] = []
    excluded_reasons: dict[str, list[str]] = {}
    agent_service_profile = is_trusted_agent_service_request(request)
    for spec in tool_specs:
        if agent_service_profile and spec.name not in _AGENT_SERVICE_TOOL_NAMES:
            excluded_reasons[spec.name] = ["entry_profile_not_exposed"]
            continue
        policy = ToolPolicyInterpreter().view_for_spec(spec)
        missing_env = [name for name in policy.requires_env if not os.environ.get(name)]
        if missing_env:
            excluded_reasons[spec.name] = [
                f"missing_required_env:{name}" for name in missing_env
            ]
            continue
        if policy.skill_only and spec.name not in active_skill_tools:
            excluded_reasons[spec.name] = ["skill_activation_required"]
            continue
        explicitly_enabled = (
            spec.name in explicit_tools
            or bool(policy.toolset and policy.toolset in explicit_toolsets)
            or spec.name in active_skill_tools
        )
        if not policy.enabled_by_default and not explicitly_enabled:
            excluded_reasons[spec.name] = ["disabled_by_default"]
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


def _visibility_overrides(request: UserRequest) -> tuple[set[str], set[str], set[str]]:
    payload = request.metadata.get("tool_visibility")
    payload = payload if isinstance(payload, dict) else {}
    return (
        set(_string_list(payload.get("enabled_tools"))),
        set(_string_list(payload.get("enabled_toolsets"))),
        set(_string_list(payload.get("enabled_skills"))),
    )


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

    payload = spec.model_dump(mode="json")
    input_schema = payload.get("input_schema")
    if isinstance(input_schema, dict):
        payload["input_schema"] = _compact_prompt_input_schema(input_schema)
    policy = ToolPolicyInterpreter().view_for_spec(spec)
    compact_execution = _compact_prompt_execution(policy)
    if compact_execution:
        payload["execution"] = compact_execution
    else:
        payload.pop("execution", None)
    if (
        policy.side_effect_level in {"none", "local_read", "external_read"}
        and not policy.requires_confirmation
    ):
        payload.pop("side_effect", None)
        return payload
    compact_side_effect = {"level": policy.side_effect_level}
    if policy.requires_confirmation:
        compact_side_effect["requires_confirmation"] = True
    if policy.confirmation_kind:
        compact_side_effect["confirmation_kind"] = policy.confirmation_kind
    payload["side_effect"] = compact_side_effect
    return payload


def _compact_prompt_execution(policy: Any) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if policy.dependency_mode != "independent":
        compact["dependency_mode"] = policy.dependency_mode
    if policy.realtime_safety != "safe":
        compact["realtime_safety"] = policy.realtime_safety
    if policy.concurrency_group:
        compact["concurrency_group"] = policy.concurrency_group
    if policy.resource_writes:
        compact["resource_writes"] = list(policy.resource_writes)
    return compact


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
