"""Prompt-safe Context Compiler v1 reporting."""

from __future__ import annotations

import json
from typing import Any, Iterable

from assistant_agent.context.models import (
    AssistantContextPack,
    ContextReport,
    ContextReportSection,
    ContextSection,
    ContextSourceReport,
    ContextSourceResult,
)
from assistant_agent.tools.models import ToolSpec
from assistant_agent.tools.spec_adapters import tool_specs_to_openai_tools
from assistant_agent.runtime.chat_adapter import ChatRequest


CONTEXT_REPORT_VERSION = "context_report_v1"
CONTEXT_REPORT_SECTION_NAMES = (
    "system_prompt",
    "request",
    "session_summary",
    "recent_transcript",
    "memory",
    "realtime_task_state",
    "realtime_video_context",
    "durable_task_state",
    "plan_state",
    "tool_observations",
    "tool_schema",
    "tool_capability",
)


def build_context_report(
    pack: AssistantContextPack,
    *,
    system_prompt: str | None = None,
    selected_tool_specs: Iterable[ToolSpec] | None = None,
    compiled_request: ChatRequest | None = None,
) -> ContextReport:
    """Build a redacted context report for the material sent to a provider."""

    selected_specs = _selected_tool_specs(pack, selected_tool_specs)
    sections = _empty_sections()
    compiled_system_prompt = _compiled_system_prompt(compiled_request) or system_prompt or ""
    sections["system_prompt"] = ContextReportSection(
        chars=len(compiled_system_prompt),
        tokens=None,
        item_count=1 if compiled_system_prompt else 0,
        included=bool(compiled_system_prompt),
        source="ChatRequest.messages[0]" if compiled_request is not None else ("system_prompt_policy" if system_prompt else "not_available"),
    )
    sections["request"] = ContextReportSection(
        chars=len(pack.request.text or ""),
        tokens=_positive_or_none(pack.budget.request_tokens),
        item_count=1 if pack.request.text else 0,
        included=bool(pack.request.text),
        source="UserRequest.text",
    )
    sections["session_summary"] = ContextReportSection(
        chars=_json_chars(pack.context_summary.model_dump(mode="json")) if pack.context_summary is not None else 0,
        tokens=None,
        item_count=pack.context_summary.source_turn_count if pack.context_summary is not None else 0,
        included=pack.context_summary is not None,
        compacted=pack.context_summary is not None,
        source="context_summary" if pack.context_summary is not None else "not_available",
    )
    sections["recent_transcript"] = ContextReportSection(
        chars=len(pack.conversation_text),
        tokens=_positive_or_none(pack.budget.conversation_tokens),
        item_count=_source_count(pack, "conversation_recent_turns") or _source_count(pack, "conversation_turns"),
        included=bool(pack.conversation_text),
        compacted="conversation_context_compacted" in pack.budget.compression_reasons,
        trimmed="conversation" in pack.budget.trimmed_sections,
        source="conversation_context_text",
    )
    memory_item_ids = _memory_item_ids(pack)
    sections["memory"] = ContextReportSection(
        chars=len(pack.memory_text),
        tokens=_positive_or_none(pack.budget.memory_tokens),
        item_count=_source_count(pack, "memory_items") or len(memory_item_ids),
        included=bool(pack.memory_text or memory_item_ids),
        trimmed="memory" in pack.budget.trimmed_sections,
        source="ContextBuilder.session_memory_snapshot",
    )
    sections["realtime_task_state"] = ContextReportSection(
        chars=_json_chars(pack.realtime_task_state) if pack.realtime_task_state else 0,
        tokens=None,
        item_count=len(pack.realtime_task_state) if isinstance(pack.realtime_task_state, dict) else 0,
        included=pack.realtime_task_state is not None,
        source="request.metadata.realtime_task_state",
    )
    sections["realtime_video_context"] = ContextReportSection(
        chars=(
            _json_chars(pack.realtime_video_context.model_dump(mode="json"))
            if pack.realtime_video_context is not None
            else 0
        ),
        tokens=_positive_or_none(pack.budget.realtime_video_context_tokens),
        item_count=1 if pack.realtime_video_context is not None else 0,
        included=pack.realtime_video_context is not None,
        source="RealtimeVideoMemoryStore",
    )
    sections["durable_task_state"] = ContextReportSection(
        chars=_json_chars(pack.durable_task_state) if pack.durable_task_state else 0,
        tokens=_positive_or_none(pack.budget.durable_task_state_tokens),
        item_count=1 if pack.durable_task_state is not None else 0,
        included=pack.durable_task_state is not None,
        trimmed="durable_task_state" in pack.budget.trimmed_sections,
        source="trusted_runtime.durable_task_snapshot",
    )
    sections["plan_state"] = ContextReportSection(
        chars=_plan_chars(pack),
        tokens=_positive_or_none(pack.budget.plan_tokens),
        item_count=1 if _plan_chars(pack) > 0 else 0,
        included=_plan_chars(pack) > 0,
        source="AgentState.plan_state",
    )
    sections["tool_observations"] = ContextReportSection(
        chars=_json_chars(pack.observations),
        tokens=_positive_or_none(pack.budget.observations_tokens),
        item_count=len(pack.observations),
        included=bool(pack.observations),
        compacted=any(observation.get("compacted") is True for observation in pack.observations),
        trimmed="observations" in pack.budget.trimmed_sections,
        source="ToolObservation.prompt_copy",
    )
    compiled_tools = compiled_request.tools if compiled_request is not None else tool_specs_to_openai_tools(selected_specs)
    sections["tool_schema"] = ContextReportSection(
        chars=_json_chars(compiled_tools),
        tokens=_positive_or_none(pack.budget.tool_spec_tokens),
        item_count=len(selected_specs),
        included=bool(selected_specs),
        source="ChatRequest.tools",
        notes=_tool_schema_notes(pack, selected_specs, selected_tool_specs),
    )
    sections["tool_capability"] = ContextReportSection(
        chars=_json_chars([capability.model_dump(mode="json") for capability in pack.tool_capabilities]),
        tokens=None,
        item_count=len(pack.tool_capabilities),
        included=bool(pack.tool_capabilities),
        source="ToolCapabilityCatalog",
    )
    compiled_message_chars = _json_chars(compiled_request.messages) if compiled_request is not None else 0
    compiled_tool_schema_chars = _json_chars(compiled_tools) if compiled_request is not None else 0
    compiled_response_format_chars = (
        _json_chars(compiled_request.response_format)
        if compiled_request is not None and compiled_request.response_format is not None
        else 0
    )
    section_total = sum(section.chars for section in sections.values())
    token_preflight = pack.request.metadata.get("context_token_preflight")
    if not isinstance(token_preflight, dict):
        token_preflight = {}
    preflight_tokens = _int_value(token_preflight.get("input_tokens"))
    effective_input_limit = _int_value(
        token_preflight.get("effective_input_limit")
    )
    rolling_compacted = (
        pack.request.metadata.get("context_compaction_applied") is True
    )
    compression_stage = "compacted" if rolling_compacted else pack.budget.compression_stage
    compression_reasons = list(pack.budget.compression_reasons)
    if rolling_compacted and "context_token_usage_high" not in compression_reasons:
        compression_reasons.append("context_token_usage_high")
    return ContextReport(
        sections=sections,
        total_chars=(
            compiled_message_chars + compiled_tool_schema_chars + compiled_response_format_chars
            if compiled_request is not None
            else section_total
        ),
        max_chars=pack.budget.max_chars,
        total_tokens=preflight_tokens or pack.budget.total_tokens,
        max_tokens=effective_input_limit or pack.budget.max_tokens,
        selected_tool_names=[spec.name for spec in selected_specs],
        memory_item_ids=memory_item_ids,
        skill_report=pack.skill_report,
        context_sources=pack.context_source_report,
        compression_stage=compression_stage,
        compression_reasons=compression_reasons,
        was_compacted=(
            rolling_compacted
            or pack.budget.compaction_triggered
            or pack.budget.compression_stage != "none"
        ),
        accounting_basis="compiled_chat_request" if compiled_request is not None else "section_estimate",
        budget_estimated_chars=pack.budget.total_chars,
        compiled_message_chars=compiled_message_chars,
        compiled_tool_schema_chars=compiled_tool_schema_chars,
        compiled_response_format_chars=compiled_response_format_chars,
    )


def _compiled_system_prompt(request: ChatRequest | None) -> str:
    if request is None:
        return ""
    for message in request.messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def build_context_source_report(
    result: ContextSourceResult,
    sections: Iterable[ContextSection],
) -> ContextSourceReport:
    """Build content-free accounting for governed context sources."""

    active_sections = list(sections)
    count_by_kind: dict[str, int] = {}
    chars_by_authority: dict[str, int] = {}
    chars_by_stability: dict[str, int] = {}
    for section in active_sections:
        count_by_kind[section.kind] = count_by_kind.get(section.kind, 0) + 1
        chars_by_authority[section.authority] = (
            chars_by_authority.get(section.authority, 0) + len(section.content)
        )
        chars_by_stability[section.stability] = (
            chars_by_stability.get(section.stability, 0) + len(section.content)
        )
    issue_codes = _unique_strings(issue.code for issue in result.issues)
    explicit_rejections = sum(
        1
        for issue in result.issues
        if issue.code
        in {
            "context_source_duplicate_section_id",
            "context_source_sensitive_section_rejected",
        }
    )
    omitted_section_count = max(
        0,
        len(result.sections) - len(active_sections),
        explicit_rejections,
    )
    if result.issues and not active_sections and not result.used_last_known_good:
        omitted_section_count = max(1, omitted_section_count)
    return ContextSourceReport(
        count_by_kind=count_by_kind,
        chars_by_authority=chars_by_authority,
        chars_by_stability=chars_by_stability,
        source_issue_count=len(result.issues),
        source_issue_codes=issue_codes,
        used_last_known_good=result.used_last_known_good,
        source_versions_changed=sum(
            1
            for section in result.sections
            if "source_version_changed" in section.notes
        ),
        omitted_section_count=omitted_section_count,
    )


def context_report_from_trace_context_summary(context: dict[str, Any]) -> ContextReport:
    """Return a best-effort report for older traces that only have context summaries."""

    budget = context.get("budget") if isinstance(context.get("budget"), dict) else {}
    source_counts = context.get("source_counts") if isinstance(context.get("source_counts"), dict) else {}
    tool_catalog = context.get("tool_catalog") if isinstance(context.get("tool_catalog"), dict) else {}
    selected_tool_names = _string_list(tool_catalog.get("selected_tool_names"))
    fallback_used = tool_catalog.get("fallback_used") is True
    compression_reasons = _string_list(budget.get("compression_reasons"))
    compression_stage = _string_value(budget.get("compression_stage")) or "none"
    trimmed_sections = set(_string_list(budget.get("trimmed_sections")))
    sections = _empty_sections()
    sections["system_prompt"] = ContextReportSection(
        source="legacy_context_summary",
        notes=["legacy_context_summary_no_system_prompt"],
    )
    sections["request"] = ContextReportSection(
        chars=_int_value(budget.get("request_chars")),
        tokens=_positive_or_none(_int_value(budget.get("request_tokens"))),
        item_count=1 if _int_value(budget.get("request_chars")) > 0 else 0,
        included=_int_value(budget.get("request_chars")) > 0,
        source="legacy_context_summary.budget",
    )
    sections["session_summary"] = ContextReportSection(
        included=context.get("context_summary_present") is True,
        compacted=context.get("context_summary_present") is True,
        item_count=1 if context.get("context_summary_present") is True else 0,
        source="legacy_context_summary.context_summary_present",
    )
    sections["recent_transcript"] = ContextReportSection(
        chars=_int_value(budget.get("conversation_chars")),
        tokens=_positive_or_none(_int_value(budget.get("conversation_tokens"))),
        item_count=_int_value(source_counts.get("conversation_recent_turns"))
        or _int_value(source_counts.get("conversation_turns")),
        included=_int_value(budget.get("conversation_chars")) > 0,
        compacted="conversation_context_compacted" in compression_reasons,
        trimmed="conversation" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    sections["memory"] = ContextReportSection(
        chars=_int_value(budget.get("memory_chars")),
        tokens=_positive_or_none(_int_value(budget.get("memory_tokens"))),
        item_count=_int_value(source_counts.get("memory_items")),
        included=_int_value(budget.get("memory_chars")) > 0 or _int_value(source_counts.get("memory_items")) > 0,
        trimmed="memory" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    sections["realtime_task_state"] = ContextReportSection(
        chars=_int_value(budget.get("realtime_task_state_chars")),
        item_count=_int_value(source_counts.get("realtime_task_state")),
        included=_int_value(source_counts.get("realtime_task_state")) > 0,
        source="legacy_context_summary.budget",
    )
    sections["realtime_video_context"] = ContextReportSection(
        chars=_int_value(budget.get("realtime_video_context_chars")),
        tokens=_positive_or_none(_int_value(budget.get("realtime_video_context_tokens"))),
        item_count=_int_value(source_counts.get("realtime_video_context")),
        included=_int_value(source_counts.get("realtime_video_context")) > 0,
        source="legacy_context_summary.budget",
    )
    sections["durable_task_state"] = ContextReportSection(
        chars=_int_value(budget.get("durable_task_state_chars")),
        tokens=_positive_or_none(_int_value(budget.get("durable_task_state_tokens"))),
        item_count=_int_value(source_counts.get("durable_task_state")),
        included=_int_value(source_counts.get("durable_task_state")) > 0,
        trimmed="durable_task_state" in trimmed_sections,
        source="trusted_runtime.durable_task_snapshot",
    )
    sections["plan_state"] = ContextReportSection(
        chars=_int_value(budget.get("plan_chars")),
        tokens=_positive_or_none(_int_value(budget.get("plan_tokens"))),
        item_count=1 if _int_value(budget.get("plan_chars")) > 0 else 0,
        included=_int_value(budget.get("plan_chars")) > 0,
        source="legacy_context_summary.budget",
    )
    sections["tool_observations"] = ContextReportSection(
        chars=_int_value(budget.get("observations_chars")),
        tokens=_positive_or_none(_int_value(budget.get("observations_tokens"))),
        item_count=_int_value(source_counts.get("observations")),
        included=_int_value(source_counts.get("observations")) > 0,
        compacted=_int_value((context.get("compaction") or {}).get("compacted_observations")) > 0
        if isinstance(context.get("compaction"), dict)
        else False,
        trimmed="observations" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    sections["tool_schema"] = ContextReportSection(
        chars=_int_value(budget.get("tool_spec_chars")),
        tokens=_positive_or_none(_int_value(budget.get("tool_spec_tokens"))),
        item_count=_int_value(source_counts.get("prompt_tool_specs")) or len(selected_tool_names),
        included=bool(selected_tool_names or _int_value(source_counts.get("prompt_tool_specs"))),
        source="legacy_context_summary.tool_catalog",
        notes=["fallback_visible_tool_list"] if fallback_used else [],
    )
    sections["tool_capability"] = ContextReportSection(
        chars=_int_value(budget.get("tool_capability_chars")),
        item_count=_int_value(source_counts.get("tool_capabilities")),
        included=_int_value(source_counts.get("tool_capabilities")) > 0,
        source="legacy_context_summary.budget",
    )
    return ContextReport(
        sections=sections,
        total_chars=_int_value(budget.get("total_chars")) or sum(section.chars for section in sections.values()),
        max_chars=_int_value(budget.get("max_chars")),
        total_tokens=_int_value(budget.get("total_tokens")),
        max_tokens=_int_value(budget.get("max_tokens")),
        selected_tool_names=selected_tool_names,
        memory_item_ids=_string_list(context.get("memory_item_ids")),
        context_sources=_context_source_report(context.get("context_sources")),
        compression_stage=compression_stage,
        compression_reasons=compression_reasons,
        was_compacted=bool(
            budget.get("compaction_triggered") is True
            or compression_stage != "none"
            or compression_reasons
        ),
    )


def _empty_sections() -> dict[str, ContextReportSection]:
    return {name: ContextReportSection() for name in CONTEXT_REPORT_SECTION_NAMES}


def _selected_tool_specs(
    pack: AssistantContextPack,
    selected_tool_specs: Iterable[ToolSpec] | None,
) -> list[ToolSpec]:
    if selected_tool_specs is not None:
        return list(selected_tool_specs)
    if pack.prompt_tool_specs:
        return list(pack.prompt_tool_specs)
    if pack.run_tool_catalog.available_tool_names:
        return []
    return list(pack.tool_specs)


def _tool_schema_notes(
    pack: AssistantContextPack,
    selected_specs: list[ToolSpec],
    explicit_selected_specs: Iterable[ToolSpec] | None,
) -> list[str]:
    fallback_used = pack.tool_catalog_summary.fallback_used
    if (
        explicit_selected_specs is None
        and not pack.run_tool_catalog.available_tool_names
        and not pack.prompt_tool_specs
        and pack.tool_specs
        and selected_specs
    ):
        fallback_used = True
    return ["fallback_visible_tool_list"] if fallback_used else []


def _memory_item_ids(pack: AssistantContextPack) -> list[str]:
    ids = list(pack.memory_source_ids)
    if ids:
        return ids
    discovered: list[str] = []
    for block in pack.memory_blocks:
        items = block.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            memory_id = item.get("memory_id")
            if isinstance(memory_id, str) and memory_id and memory_id not in discovered:
                discovered.append(memory_id)
    return discovered


def _plan_chars(pack: AssistantContextPack) -> int:
    plan = pack.plan_state
    if plan.current_plan is None and plan.plan_status == "none":
        return 0
    return _json_chars(plan.model_dump(mode="json"))


def _source_count(pack: AssistantContextPack, key: str) -> int:
    return _int_value(pack.source_counts.get(key))


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _positive_or_none(value: int) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _context_source_report(value: Any) -> ContextSourceReport:
    if not isinstance(value, dict):
        return ContextSourceReport()
    try:
        return ContextSourceReport.model_validate(value)
    except Exception:
        return ContextSourceReport()


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


def _unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
