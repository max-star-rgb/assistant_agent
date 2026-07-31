"""Trace helpers for assistant context pack construction."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from assistant_agent.runtime.state import AgentState
from assistant_agent.context.models import AssistantContextPack
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolSpec
from assistant_agent.context.builder import build_assistant_context_pack
from assistant_agent.context.compactor import ContextCompactor
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.context.token_counter import ContextTokenCounter
from assistant_agent.context.report import (
    build_context_report,
    context_report_trace_payload,
)
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.observability.trace_store import TraceStore, append_observability_event, new_span_id, sanitize_trace_value


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
    context_token_counter: ContextTokenCounter | None = None,
    context_window_policy: ContextWindowPolicy | None = None,
    registry_generation: str | None = None,
    native_calls: list[dict[str, Any]] | None = None,
    current_location: str | None = None,
    answer_only: bool = False,
    supports_developer_role: bool = False,
    build_reason: str = "iteration_initial",
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
            "build_reason": build_reason,
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
            registry_generation=registry_generation,
        )
        compilation = PromptCompiler().compile(
            PromptCompileRequest(
                user_id=state.user_id,
                session_id=state.session_id,
                mode=PromptCompileMode.NATIVE_TOOL,
                user_query_fallback="native_tools assistant turn",
                context_pack=pack,
                observations=tuple(pack.observations),
                native_calls=tuple(native_calls or ()),
                tool_call_id_prefix="call_",
                current_location=current_location,
                answer_only=answer_only,
                supports_developer_role=supports_developer_role,
            )
        )
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="context.build.finished",
            observation_type="span",
            observation_name="context.compile",
            observation_scope="iteration",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started_at),
            span_id=span_id,
            attributes={
                "iteration": iteration + 1,
                "max_iterations": max_iterations,
                "build_reason": build_reason,
            },
            error={"code": "context_build_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise
    compiled_input_tokens = None
    effective_input_limit = None
    if context_token_counter is not None and context_window_policy is not None:
        compiled_input_tokens = context_token_counter.count_chat_request(
            compilation.chat_request
        )
        effective_input_limit = context_window_policy.evaluate(
            compiled_input_tokens,
            reserved_output_tokens=compilation.chat_request.max_tokens,
        ).effective_input_limit
    context_report = build_context_report(
        pack,
        selected_tool_specs=compilation.selected_tool_specs,
        compiled_request=compilation.chat_request,
        compiled_input_tokens=compiled_input_tokens,
        effective_input_limit=effective_input_limit,
    )
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="context.build.finished",
        observation_type="span",
        observation_name="context.compile",
        observation_scope="iteration",
        node_name=node_name,
        status="succeeded",
        latency_ms=_elapsed_ms(started_at),
        span_id=span_id,
        output_summary={
            "output_kind": "prompt_safe_context_compilation_report",
            "build_reason": build_reason,
            "compiled_request_shape": _compiled_request_shape(compilation.chat_request),
            "compiled_request_ref": {
                "observation_name": "llm.chat",
                "field": "input",
                "iteration": iteration + 1,
            },
            "context": context_trace_summary(pack),
            "context_report_v2": context_report_trace_payload(context_report),
        },
        attributes={
            "iteration": iteration + 1,
            "max_iterations": max_iterations,
            "build_reason": build_reason,
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


def _compiled_request_shape(request: Any) -> dict[str, Any]:
    """Describe the compiled Provider request without duplicating prompt content."""

    return {
        "message_count": len(request.messages),
        "message_roles": [
            str(message.get("role") or "unknown")
            for message in request.messages
        ],
        "tool_count": len(request.tools),
        "response_format_present": request.response_format is not None,
    }


def context_trace_summary(pack: AssistantContextPack) -> dict[str, Any]:
    """Return the prompt-safe public context summary used by traces and metrics."""

    return {
        "context_schema_version": "context_observability_v1",
        "budget": pack.budget.model_dump(mode="json"),
        "source_counts": pack.source_counts,
        "compaction": _context_compaction_summary(pack.observations),
        "tool_catalog": pack.tool_catalog_summary.model_dump(mode="json"),
        "run_tool_catalog": pack.run_tool_catalog.model_dump(mode="json"),
        "active_skill_ids": list(pack.active_skill_ids),
        "context_sources": pack.context_source_report.model_dump(mode="json"),
        "compactor_type": pack.compactor_type,
        "context_summary_present": pack.context_summary is not None,
        "realtime_video": _realtime_video_trace(pack),
    }


def _realtime_video_trace(pack: AssistantContextPack) -> dict[str, Any]:
    context = pack.realtime_video_context
    if context is None:
        return {
            "present": False,
            "status": "unavailable",
            "waited_for_initial_snapshot": pack.request.metadata.get(
                "realtime_video_waited_for_initial_snapshot"
            )
            is True,
        }
    trace = {
        "present": True,
        "status": context.status,
        "snapshot_age_ms": context.snapshot_age_ms,
        "snapshot_sequence": context.snapshot_sequence,
        "observation_latency_ms": context.observation_latency_ms,
        "provider": context.provider,
        "model": context.model,
        "pending_count": context.pending_count,
        "in_flight": context.in_flight,
        "waited_for_initial_snapshot": pack.request.metadata.get(
            "realtime_video_waited_for_initial_snapshot"
        )
        is True,
    }
    optional_values = {
        "target_sequence": context.target_sequence,
        "sequence_gap": context.sequence_gap,
        "frame_capture_age_ms": context.frame_capture_age_ms,
        "snapshot_publish_age_ms": context.snapshot_publish_age_ms,
        "freshness_waited_ms": _optional_metadata_int(
            pack.request.metadata,
            "realtime_video_freshness_waited_ms",
        ),
        "freshness_satisfied": _optional_metadata_bool(
            pack.request.metadata,
            "realtime_video_freshness_satisfied",
        ),
        "transport": context.transport,
        "session_generation": context.session_generation,
        "connection_reused": context.connection_reused,
        "reconnect_count": context.reconnect_count,
        "completed_sequence": context.completed_sequence,
        "first_delta_latency_ms": context.first_delta_latency_ms,
        "total_observation_latency_ms": context.total_observation_latency_ms,
    }
    trace.update({key: value for key, value in optional_values.items() if value is not None})
    return trace


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


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


def _optional_metadata_int(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_metadata_bool(metadata: dict[str, Any], key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None
