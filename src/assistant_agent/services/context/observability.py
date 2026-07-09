"""Trace helpers for assistant context pack construction."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.context import AssistantContextPack
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.context.builder import build_assistant_context_pack
from assistant_agent.services.context.compactor import ContextCompactor
from assistant_agent.services.trace_store import TraceStore, append_observability_event, new_span_id, sanitize_trace_value


def build_traced_assistant_context_pack(
    *,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    request: UserRequest | None = None,
    observations: list[dict[str, Any]] | None = None,
    tool_specs: list[ToolSpec] | None = None,
    iteration: int,
    max_iterations: int,
    memory_summaries: list[str] | None = None,
    memory_text: str | None = None,
    context_compactor: ContextCompactor | None = None,
) -> AssistantContextPack:
    """Build an assistant context pack and emit redacted canonical trace events."""

    active_request = request or state.request
    span_id = new_span_id()
    started_at = perf_counter()
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="context.build.started",
        node_name=node_name,
        status="started",
        span_id=span_id,
        attributes={
            "iteration": iteration + 1,
            "max_iterations": max_iterations,
            "observation_count": len(observations or []),
            "tool_spec_count": len(tool_specs or []),
            "request_metadata_keys": sorted(active_request.metadata.keys()),
        },
    )
    try:
        pack = build_assistant_context_pack(
            state=state,
            request=active_request,
            observations=observations,
            tool_specs=tool_specs,
            iteration=iteration,
            max_iterations=max_iterations,
            memory_summaries=memory_summaries,
            memory_text=memory_text,
            context_compactor=context_compactor,
        )
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="context.build.finished",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started_at),
            span_id=span_id,
            attributes={"iteration": iteration + 1, "max_iterations": max_iterations},
            error={"code": "context_build_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise

    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="context.build.finished",
        node_name=node_name,
        status="succeeded",
        latency_ms=_elapsed_ms(started_at),
        span_id=span_id,
        output_summary={"context": context_trace_summary(pack)},
        attributes={
            "iteration": iteration + 1,
            "max_iterations": max_iterations,
            "prompt_tool_count": len(pack.prompt_tool_specs),
            "observation_count": len(pack.observations),
            "context_usage_ratio": pack.budget.context_usage_ratio,
            "compaction_triggered": pack.budget.compaction_triggered,
            "compression_stage": pack.budget.compression_stage,
            "total_chars": pack.budget.total_chars,
            "total_tokens": pack.budget.total_tokens,
        },
    )
    return pack


def context_trace_summary(pack: AssistantContextPack) -> dict[str, Any]:
    """Return the prompt-safe public context summary used by traces and metrics."""

    return {
        "context_schema_version": "context_observability_v1",
        "budget": pack.budget.model_dump(mode="json"),
        "source_counts": pack.source_counts,
        "compaction": _context_compaction_summary(pack.observations),
        "tool_catalog": pack.tool_catalog_summary.model_dump(mode="json"),
        "skill_report_v1": pack.skill_report.model_dump(mode="json"),
        "compactor_type": pack.compactor_type,
        "context_summary_present": pack.context_summary is not None,
        "memory_promotion_candidates": _metadata_int(pack.request.metadata, "memory_promotion_candidates"),
        "memory_promotion_written": _metadata_int(pack.request.metadata, "memory_promotion_written"),
        "memory_tool_selection": _memory_tool_selection_trace(pack.request.metadata),
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _memory_tool_selection_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    selection = metadata.get("memory_tool_selection")
    if not isinstance(selection, dict):
        return {}
    return {
        "strategy": selection.get("strategy"),
        "action": selection.get("action"),
        "selected_memory_tool": selection.get("selected_memory_tool"),
        "keyword_signals": selection.get("keyword_signals", []),
        "missed_signals": selection.get("missed_signals", []),
        "candidate_mode": selection.get("candidate_mode"),
        "auto_write": selection.get("auto_write"),
        "vector_shadow_hit_count": _selection_vector_hit_count(selection),
    }


def _selection_vector_hit_count(selection: dict[str, Any]) -> int:
    signal = selection.get("vector_shadow_signal")
    if not isinstance(signal, dict):
        return 0
    value = signal.get("hit_count")
    return value if isinstance(value, int) and value >= 0 else 0


def _context_compaction_summary(observations: list[dict[str, Any]]) -> dict[str, int]:
    compacted_count = 0
    original_chars = 0
    compacted_chars = 0
    pruned_payload_keys = 0
    command_outputs_truncated = 0
    original_command_output_chars = 0
    compacted_command_output_chars = 0
    for observation in observations:
        compaction = observation.get("compaction")
        if not isinstance(compaction, dict):
            continue
        compacted_count += 1
        original_chars += _metadata_int(compaction, "original_chars")
        compacted_chars += _metadata_int(compaction, "compacted_chars")
        pruned_payload_keys += _list_count(compaction.get("pruned_keys"))
        pruned_payload_keys += _metadata_int(compaction, "omitted_pruned_keys_count")
        command_outputs_truncated += _list_count(compaction.get("command_output_keys"))
        command_outputs_truncated += _metadata_int(compaction, "omitted_command_output_keys_count")
        original_command_output_chars += _metadata_int(compaction, "original_command_output_chars")
        compacted_command_output_chars += _metadata_int(compaction, "compacted_command_output_chars")
    return {
        "compacted_observations": compacted_count,
        "original_observation_chars": original_chars,
        "compacted_observation_chars": compacted_chars,
        "pruned_payload_keys": pruned_payload_keys,
        "command_outputs_truncated": command_outputs_truncated,
        "original_command_output_chars": original_command_output_chars,
        "compacted_command_output_chars": compacted_command_output_chars,
    }


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else 0
