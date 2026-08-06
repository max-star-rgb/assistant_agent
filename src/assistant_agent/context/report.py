"""Prompt-safe Context Compiler v2 reporting and legacy conversion."""

from __future__ import annotations

import json
from typing import Any, Iterable, Literal

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
from assistant_agent.tools.observation import native_tool_observation_payload
from assistant_agent.runtime.chat_adapter import ChatRequest


CONTEXT_REPORT_VERSION = "context_report_v2"
CONTEXT_REPORT_SECTION_NAMES = (
    "system_prompt",
    "developer_prompt",
    "request",
    "proactive_session_events",
    "session_summary",
    "recent_transcript",
    "memory",
    "realtime_video_context",
    "durable_task_state",
    "plan_state",
    "tool_observations",
    "tool_schema",
)


def build_context_report(
    pack: AssistantContextPack,
    *,
    system_prompt: str | None = None,
    selected_tool_specs: Iterable[ToolSpec] | None = None,
    compiled_request: ChatRequest | None = None,
    compiled_input_tokens: int | None = None,
    effective_input_limit: int | None = None,
) -> ContextReport:
    """Build a redacted context report for the material sent to a provider."""

    selected_specs = _selected_tool_specs(pack, selected_tool_specs)
    sections: dict[str, ContextReportSection] = {}
    compiled_system_prompt = _compiled_system_prompt(compiled_request) or system_prompt or ""
    if compiled_system_prompt:
        sections["system_prompt"] = ContextReportSection(
            chars=len(compiled_system_prompt),
            source=(
                "ChatRequest.messages[0]"
                if compiled_request is not None
                else "system_prompt_policy"
            ),
        )
    compiled_developer_prompt, developer_message_index = _compiled_role_prompt(
        compiled_request,
        "developer",
    )
    if compiled_developer_prompt:
        sections["developer_prompt"] = ContextReportSection(
            chars=len(compiled_developer_prompt),
            source=f"ChatRequest.messages[{developer_message_index}]",
        )
    if pack.request.text:
        sections["request"] = ContextReportSection(
            chars=len(pack.request.text),
            estimated_tokens=_positive_or_none(pack.budget.request_tokens),
            source="UserRequest.text",
        )
    proactive_events = _proactive_session_events(pack.request.metadata)
    if proactive_events:
        sections["proactive_session_events"] = ContextReportSection(
            chars=len(
                json.dumps(
                    proactive_events,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
            item_count=len(proactive_events),
            source="trusted_runtime.proactive_session_events",
        )
    if pack.context_summary is not None:
        sections["session_summary"] = ContextReportSection(
            chars=_json_chars(pack.context_summary.model_dump(mode="json")),
            item_count=pack.context_summary.source_turn_count,
            compaction="rolling_summary",
            source="context_summary",
        )
    if pack.conversation_text or "conversation" in pack.budget.trimmed_sections:
        sections["recent_transcript"] = ContextReportSection(
            chars=len(pack.conversation_text),
            estimated_tokens=_positive_or_none(pack.budget.conversation_tokens),
            item_count=_positive_or_none(
                _source_count(pack, "conversation_recent_turns")
                or _source_count(pack, "conversation_turns")
            ),
            compaction=(
                "rolling_summary"
                if "conversation_context_compacted" in pack.budget.compression_reasons
                else None
            ),
            trimmed="conversation" in pack.budget.trimmed_sections,
            source="conversation_context_text",
        )
    memory_item_ids = _memory_item_ids(pack)
    if pack.memory_text or memory_item_ids or "memory" in pack.budget.trimmed_sections:
        sections["memory"] = ContextReportSection(
            chars=len(pack.memory_text),
            estimated_tokens=_positive_or_none(pack.budget.memory_tokens),
            item_count=_positive_or_none(
                _source_count(pack, "memory_items") or len(memory_item_ids)
            ),
            trimmed="memory" in pack.budget.trimmed_sections,
            source="ContextBuilder.session_memory_snapshot",
        )
    if pack.realtime_video_context is not None:
        sections["realtime_video_context"] = ContextReportSection(
            chars=_json_chars(pack.realtime_video_context.model_dump(mode="json")),
            estimated_tokens=_positive_or_none(
                pack.budget.realtime_video_context_tokens
            ),
            item_count=1,
            source="RealtimeVideoMemoryStore",
        )
    if (
        pack.durable_task_state is not None
        or "durable_task_state" in pack.budget.trimmed_sections
    ):
        sections["durable_task_state"] = ContextReportSection(
            chars=_json_chars(pack.durable_task_state) if pack.durable_task_state else 0,
            estimated_tokens=_positive_or_none(
                pack.budget.durable_task_state_tokens
            ),
            item_count=1 if pack.durable_task_state is not None else None,
            trimmed="durable_task_state" in pack.budget.trimmed_sections,
            source="trusted_runtime.durable_task_snapshot",
        )
    plan_chars = _plan_chars(pack)
    if plan_chars:
        sections["plan_state"] = ContextReportSection(
            chars=plan_chars,
            estimated_tokens=_positive_or_none(pack.budget.plan_tokens),
            item_count=1,
            source="AgentState.plan_state",
        )
    if pack.observations or "observations" in pack.budget.trimmed_sections:
        sections["tool_observations"] = ContextReportSection(
            chars=_json_chars(
                [
                    native_tool_observation_payload(observation)
                    for observation in pack.observations
                ]
            ),
            estimated_tokens=_positive_or_none(pack.budget.observations_tokens),
            item_count=len(pack.observations),
            compaction=(
                "prompt_projection"
                if any(
                    observation.get("compacted") is True
                    for observation in pack.observations
                )
                else None
            ),
            trimmed="observations" in pack.budget.trimmed_sections,
            source="ToolObservation.prompt_copy",
        )
    compiled_tools = compiled_request.tools if compiled_request is not None else tool_specs_to_openai_tools(selected_specs)
    if selected_specs:
        sections["tool_schema"] = ContextReportSection(
            chars=_json_chars(compiled_tools),
            estimated_tokens=_positive_or_none(pack.budget.tool_spec_tokens),
            item_count=len(selected_specs),
            source="ChatRequest.tools",
            notes=_tool_schema_notes(pack, selected_specs, selected_tool_specs),
        )
    compiled_message_chars = (
        _json_chars(compiled_request.messages)
        if compiled_request is not None
        else None
    )
    compiled_tool_schema_chars = (
        _json_chars(compiled_tools) if compiled_request is not None else None
    )
    compiled_response_format_chars = (
        _json_chars(compiled_request.response_format)
        if compiled_request is not None and compiled_request.response_format is not None
        else (0 if compiled_request is not None else None)
    )
    token_preflight = pack.request.metadata.get("context_token_preflight")
    if not isinstance(token_preflight, dict):
        token_preflight = {}
    preflight_tokens = (
        _optional_nonnegative_int(compiled_input_tokens)
        if compiled_input_tokens is not None
        else _optional_nonnegative_int(token_preflight.get("input_tokens"))
    )
    resolved_input_limit = (
        _optional_positive_int(effective_input_limit)
        if effective_input_limit is not None
        else _optional_positive_int(token_preflight.get("effective_input_limit"))
    )
    rolling_compacted = (
        pack.request.metadata.get("context_compaction_applied") is True
    )
    compression_stage = "compacted" if rolling_compacted else pack.budget.compression_stage
    compression_reasons = list(pack.budget.compression_reasons)
    if rolling_compacted and "context_token_usage_high" not in compression_reasons:
        compression_reasons.append("context_token_usage_high")
    compiled_request_chars = (
        compiled_message_chars
        + compiled_tool_schema_chars
        + compiled_response_format_chars
        if (
            compiled_message_chars is not None
            and compiled_tool_schema_chars is not None
            and compiled_response_format_chars is not None
        )
        else None
    )
    context_sources = (
        pack.context_source_report
        if _has_context_source_evidence(pack.context_source_report)
        else None
    )
    return ContextReport(
        schema_version=CONTEXT_REPORT_VERSION,
        sections=sections,
        compiled_accounting_status=(
            "available" if compiled_request is not None else "unavailable"
        ),
        compiled_request_chars=compiled_request_chars,
        compiled_message_chars=compiled_message_chars,
        compiled_tool_schema_chars=compiled_tool_schema_chars,
        compiled_response_format_chars=compiled_response_format_chars,
        token_accounting_status=(
            "available"
            if preflight_tokens is not None and resolved_input_limit is not None
            else "unavailable"
        ),
        compiled_input_tokens=preflight_tokens,
        effective_input_limit=resolved_input_limit,
        selected_tool_names=[spec.name for spec in selected_specs],
        memory_item_ids=memory_item_ids,
        context_sources=context_sources,
        compression_stage=compression_stage,
        compression_reasons=compression_reasons,
        precompile_estimated_chars=pack.budget.total_chars,
        precompile_max_chars=pack.budget.max_chars,
    )


def _compiled_system_prompt(request: ChatRequest | None) -> str:
    content, _ = _compiled_role_prompt(request, "system")
    return content


def _compiled_role_prompt(
    request: ChatRequest | None,
    role: str,
) -> tuple[str, int | None]:
    if request is None:
        return "", None
    for index, message in enumerate(request.messages):
        if message.get("role") == role and isinstance(
            message.get("content"),
            str,
        ):
            return message["content"], index
    return "", None


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
    """Return a v2 best-effort report for traces with only legacy summaries."""

    budget = context.get("budget") if isinstance(context.get("budget"), dict) else {}
    source_counts = context.get("source_counts") if isinstance(context.get("source_counts"), dict) else {}
    tool_catalog = context.get("tool_catalog") if isinstance(context.get("tool_catalog"), dict) else {}
    selected_tool_names = _string_list(tool_catalog.get("selected_tool_names"))
    fallback_used = tool_catalog.get("fallback_used") is True
    compression_reasons = _string_list(budget.get("compression_reasons"))
    compression_stage = _string_value(budget.get("compression_stage")) or "none"
    trimmed_sections = set(_string_list(budget.get("trimmed_sections")))
    sections: dict[str, ContextReportSection] = {
        "system_prompt": ContextReportSection(
            chars=0,
            source="legacy_context_summary",
            notes=["legacy_context_summary_no_system_prompt"],
        )
    }
    _add_legacy_summary_section(
        sections,
        "request",
        chars=_int_value(budget.get("request_chars")),
        estimated_tokens=_positive_or_none(_int_value(budget.get("request_tokens"))),
        source="legacy_context_summary.budget",
    )
    if context.get("context_summary_present") is True:
        sections["session_summary"] = ContextReportSection(
            chars=0,
            item_count=1,
            compaction="rolling_summary",
            source="legacy_context_summary.context_summary_present",
        )
    _add_legacy_summary_section(
        sections,
        "recent_transcript",
        chars=_int_value(budget.get("conversation_chars")),
        estimated_tokens=_positive_or_none(_int_value(budget.get("conversation_tokens"))),
        item_count=_positive_or_none(
            _int_value(source_counts.get("conversation_recent_turns"))
            or _int_value(source_counts.get("conversation_turns"))
        ),
        compaction=(
            "rolling_summary"
            if "conversation_context_compacted" in compression_reasons
            else None
        ),
        trimmed="conversation" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    _add_legacy_summary_section(
        sections,
        "memory",
        chars=_int_value(budget.get("memory_chars")),
        estimated_tokens=_positive_or_none(_int_value(budget.get("memory_tokens"))),
        item_count=_positive_or_none(_int_value(source_counts.get("memory_items"))),
        trimmed="memory" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    _add_legacy_summary_section(
        sections,
        "realtime_video_context",
        chars=_int_value(budget.get("realtime_video_context_chars")),
        estimated_tokens=_positive_or_none(
            _int_value(budget.get("realtime_video_context_tokens"))
        ),
        item_count=_positive_or_none(
            _int_value(source_counts.get("realtime_video_context"))
        ),
        source="legacy_context_summary.budget",
    )
    _add_legacy_summary_section(
        sections,
        "durable_task_state",
        chars=_int_value(budget.get("durable_task_state_chars")),
        estimated_tokens=_positive_or_none(
            _int_value(budget.get("durable_task_state_tokens"))
        ),
        item_count=_positive_or_none(
            _int_value(source_counts.get("durable_task_state"))
        ),
        trimmed="durable_task_state" in trimmed_sections,
        source="trusted_runtime.durable_task_snapshot",
    )
    _add_legacy_summary_section(
        sections,
        "plan_state",
        chars=_int_value(budget.get("plan_chars")),
        estimated_tokens=_positive_or_none(_int_value(budget.get("plan_tokens"))),
        source="legacy_context_summary.budget",
    )
    compacted_observations = (
        _int_value((context.get("compaction") or {}).get("compacted_observations"))
        if isinstance(context.get("compaction"), dict)
        else 0
    )
    _add_legacy_summary_section(
        sections,
        "tool_observations",
        chars=_int_value(budget.get("observations_chars")),
        estimated_tokens=_positive_or_none(
            _int_value(budget.get("observations_tokens"))
        ),
        item_count=_positive_or_none(_int_value(source_counts.get("observations"))),
        compaction="prompt_projection" if compacted_observations > 0 else None,
        trimmed="observations" in trimmed_sections,
        source="legacy_context_summary.budget",
    )
    _add_legacy_summary_section(
        sections,
        "tool_schema",
        chars=_int_value(budget.get("tool_spec_chars")),
        estimated_tokens=_positive_or_none(_int_value(budget.get("tool_spec_tokens"))),
        item_count=_positive_or_none(
            _int_value(source_counts.get("prompt_tool_specs"))
            or len(selected_tool_names)
        ),
        source="legacy_context_summary.tool_catalog",
        notes=["fallback_visible_tool_list"] if fallback_used else [],
    )
    context_sources = _context_source_report(context.get("context_sources"))
    return ContextReport(
        schema_version=CONTEXT_REPORT_VERSION,
        sections=sections,
        compiled_accounting_status="unavailable",
        compiled_request_chars=None,
        compiled_message_chars=None,
        compiled_tool_schema_chars=None,
        compiled_response_format_chars=None,
        token_accounting_status="unavailable",
        compiled_input_tokens=None,
        effective_input_limit=None,
        selected_tool_names=selected_tool_names,
        memory_item_ids=_string_list(context.get("memory_item_ids")),
        context_sources=(
            context_sources if _has_context_source_evidence(context_sources) else None
        ),
        compression_stage=compression_stage,
        compression_reasons=compression_reasons,
        precompile_estimated_chars=_int_value(budget.get("total_chars")),
        precompile_max_chars=_int_value(budget.get("max_chars")),
    )


def context_report_v2_from_v1(report: dict[str, Any]) -> ContextReport:
    """Convert one persisted v1 compilation report into the v2 query contract."""

    raw_sections = report.get("sections")
    sections: dict[str, ContextReportSection] = {}
    if isinstance(raw_sections, dict):
        for name in CONTEXT_REPORT_SECTION_NAMES:
            raw = raw_sections.get(name)
            if not isinstance(raw, dict):
                continue
            chars = _int_value(raw.get("chars"))
            item_count = _positive_or_none(_int_value(raw.get("item_count")))
            notes = _string_list(raw.get("notes"))
            included = raw.get("included") is True
            trimmed = raw.get("trimmed") is True
            compacted = raw.get("compacted") is True
            if not (included or chars or item_count or trimmed or compacted or notes):
                continue
            sections[name] = ContextReportSection(
                chars=chars,
                estimated_tokens=_positive_or_none(_int_value(raw.get("tokens"))),
                item_count=item_count,
                compaction=(
                    "prompt_projection"
                    if compacted and name == "tool_observations"
                    else ("rolling_summary" if compacted else None)
                ),
                trimmed=trimmed,
                source=_string_value(raw.get("source")) or None,
                notes=notes,
            )
    compiled_available = report.get("accounting_basis") == "compiled_chat_request"
    total_tokens = _optional_positive_int(report.get("total_tokens"))
    max_tokens = _optional_positive_int(report.get("max_tokens"))
    context_sources = _context_source_report(report.get("context_sources"))
    return ContextReport(
        schema_version=CONTEXT_REPORT_VERSION,
        sections=sections,
        compiled_accounting_status=(
            "available" if compiled_available else "unavailable"
        ),
        compiled_request_chars=(
            _int_value(report.get("total_chars")) if compiled_available else None
        ),
        compiled_message_chars=(
            _int_value(report.get("compiled_message_chars"))
            if compiled_available
            else None
        ),
        compiled_tool_schema_chars=(
            _int_value(report.get("compiled_tool_schema_chars"))
            if compiled_available
            else None
        ),
        compiled_response_format_chars=(
            _int_value(report.get("compiled_response_format_chars"))
            if compiled_available
            else None
        ),
        token_accounting_status=(
            "available"
            if total_tokens is not None and max_tokens is not None
            else "unavailable"
        ),
        compiled_input_tokens=total_tokens,
        effective_input_limit=max_tokens,
        selected_tool_names=_string_list(report.get("selected_tool_names")),
        memory_item_ids=_string_list(report.get("memory_item_ids")),
        context_sources=(
            context_sources if _has_context_source_evidence(context_sources) else None
        ),
        compression_stage=_string_value(report.get("compression_stage")) or "none",
        compression_reasons=_string_list(report.get("compression_reasons")),
        precompile_estimated_chars=_int_value(report.get("budget_estimated_chars")),
        precompile_max_chars=_int_value(report.get("max_chars")),
    )


def context_report_trace_payload(report: ContextReport) -> dict[str, Any]:
    """Serialize a compact trace payload without dropping nested schema identity."""

    payload = report.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    if report.context_sources is not None:
        source_payload = payload.get("context_sources")
        if isinstance(source_payload, dict):
            source_payload["schema_version"] = report.context_sources.schema_version
    return payload


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


def _proactive_session_events(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = metadata.get("_trusted_proactive_session_events")
    if not isinstance(raw_events, list):
        return []
    return [event for event in raw_events if isinstance(event, dict)]


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _positive_or_none(value: int) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _optional_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _add_legacy_summary_section(
    sections: dict[str, ContextReportSection],
    name: str,
    *,
    chars: int,
    estimated_tokens: int | None = None,
    item_count: int | None = None,
    compaction: Literal["rolling_summary", "prompt_projection"] | None = None,
    trimmed: bool = False,
    source: str | None = None,
    notes: list[str] | None = None,
) -> None:
    if not (chars or item_count or compaction or trimmed or notes):
        return
    sections[name] = ContextReportSection(
        chars=chars,
        estimated_tokens=estimated_tokens,
        item_count=item_count,
        compaction=compaction,
        trimmed=trimmed,
        source=source,
        notes=notes or [],
    )


def _context_source_report(value: Any) -> ContextSourceReport:
    if not isinstance(value, dict):
        return ContextSourceReport()
    try:
        return ContextSourceReport.model_validate(value)
    except Exception:
        return ContextSourceReport()


def _has_context_source_evidence(report: ContextSourceReport) -> bool:
    return bool(
        report.count_by_kind
        or report.chars_by_authority
        or report.chars_by_stability
        or report.source_issue_count
        or report.source_issue_codes
        or report.used_last_known_good
        or report.source_versions_changed
        or report.omitted_section_count
    )


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
