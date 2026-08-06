"""Prompt-safe OpenTelemetry span mapping for text assistant traces."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.context.report import (
    context_report_trace_payload,
    context_report_v2_from_v1,
)
from assistant_agent.observability.langfuse_config import local_langfuse_trace_url
from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event, sanitize_trace_value
from assistant_agent.observability.turn_evaluator import build_turn_diagnostic
from assistant_agent.observability.turn_summary import latest_turn_summary_from_events
from assistant_agent.observability.visual_trace_content import (
    sanitize_visual_tool_result,
    sanitize_visual_trace_content,
)

if TYPE_CHECKING:
    from assistant_agent.memory.trace_content import MemoryIngestionTraceContent
    from assistant_agent.observability.trace_conversation import (
        TraceConversationView,
        TraceLlmInput,
        TraceLlmOutput,
        TraceToolObservation,
        TraceToolResult,
        TraceVlmOutput,
    )


OTEL_SPAN_SPEC_SCHEMA_VERSION = "assistant_agent_text_otel_span_spec_v1"
TEXT_MODALITY = "text"
VISION_MODALITY = "vision"

_VOICE_ATTRIBUTE_TOKENS = frozenset(
    {
        "audio",
        "tts",
        "playback",
        "speech",
        "dead_air",
        "silence",
    }
)
_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "attempt_kind",
        "attempt_count",
        "budget_ratio",
        "client_type",
        "content_export_policy",
        "capability",
        "context_usage_ratio",
        "decision_type",
        "disposition",
        "output_type",
        "error_count",
        "failure_code",
        "frame_sequence",
        "first_text_latency_ms",
        "from_phase",
        "guard_code",
        "input_tokens",
        "iteration",
        "media_count",
        "media_kind",
        "model_role",
        "next_action",
        "next_attempt",
        "output_tokens",
        "provider_latency_ms",
        "prompt_version",
        "response_present",
        "retry_exhausted",
        "execution_retry_count",
        "run_phase",
        "reason",
        "run_id",
        "route_branch",
        "runtime_action",
        "runtime_call_latency_ms",
        "search_error_count",
        "session_turn",
        "source_tool_span_id",
        "source",
        "terminal_status",
        "transport_mode",
        "tool_call_id",
        "tool_count",
        "tool_reported_latency_ms",
        "to_phase",
        "trace_kind",
        "validation_status",
        "wall_latency_ms",
    }
)
_ALLOWED_OUTPUT_KEYS = frozenset(
    {
        "artifact_id",
        "artifact_ref",
        "item_count",
        "output_ref",
        "result_count",
        "snapshot_sequence",
        "source_vision_trace_id",
        "source_vision_run_id",
        "source_vlm_span_id",
        "source_visual_record_id",
    }
)

_VISUAL_TRACE_LINK_FIELDS = (
    "source_vision_trace_id",
    "source_vision_run_id",
    "source_vlm_span_id",
    "source_visual_record_id",
    "snapshot_sequence",
)


class OtelSpanSpec(BaseModel):
    """Serializable, dependency-free span plan for a later OpenTelemetry exporter."""

    schema_version: Literal["assistant_agent_text_otel_span_spec_v1"] = (
        OTEL_SPAN_SPEC_SCHEMA_VERSION
    )
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    start_time: datetime
    end_time: datetime
    status: Literal["ok", "error", "unset"] = "unset"
    attributes: dict[str, Any] = Field(default_factory=dict)


def langfuse_trace_id(seed: str) -> str:
    """Return a native W3C trace id or map a legacy external trace id."""

    if len(seed) == 32 and seed == seed.lower():
        try:
            if int(seed, 16) != 0:
                return seed
        except ValueError:
            pass
    return sha256(seed.encode("utf-8")).digest()[:16].hex()


def build_text_otel_span_specs(
    events: Iterable[TraceEvent],
    *,
    conversation: "TraceConversationView | None" = None,
    memory_content: "MemoryIngestionTraceContent | None" = None,
) -> list[OtelSpanSpec]:
    """Map redacted text-run trace events into dependency-free OTel span specs."""

    safe_events = (
        list(events)
        if conversation is not None
        else [redact_trace_event(event) for event in events]
    )
    if not safe_events:
        return []
    started_at_by_span_id = _started_at_by_span_id(safe_events)
    root_span = _root_span(
        safe_events,
        conversation=conversation,
        started_at_by_span_id=started_at_by_span_id,
    )
    trace_attributes = _trace_attributes(safe_events)
    spans = [root_span]
    runtime_parent_id = root_span.span_id
    event_iterations = _event_iterations(safe_events)
    iteration_spans = _iteration_spans(
        safe_events,
        event_iterations=event_iterations,
        parent_span_id=runtime_parent_id,
        trace_attributes=trace_attributes,
        started_at_by_span_id=started_at_by_span_id,
    )
    spans.extend(iteration_spans.values())
    for index, event in enumerate(safe_events):
        if event.observation_type is None:
            continue
        if event.span_id == root_span.span_id:
            continue
        spans.append(
            _operation_span(
                event,
                root_span_id=root_span.span_id,
                runtime_parent_id=runtime_parent_id,
                iteration_parent_id=(
                    iteration_spans[event_iterations[index]].span_id
                    if index in event_iterations and event_iterations[index] in iteration_spans
                    else None
                ),
                trace_attributes=trace_attributes,
                conversation=conversation,
                started_at_by_span_id=started_at_by_span_id,
                memory_content=memory_content,
            )
        )
    return spans


def build_late_text_otel_span_spec(
    event: TraceEvent,
    *,
    memory_content: "MemoryIngestionTraceContent | None" = None,
) -> OtelSpanSpec:
    """Map one post-summary event without re-exporting the runtime root span."""

    safe_event = event if memory_content is not None else redact_trace_event(event)
    root_span_id = _root_span_id([safe_event])
    return _operation_span(
        safe_event,
        root_span_id=root_span_id,
        runtime_parent_id=root_span_id,
        iteration_parent_id=None,
        trace_attributes=_trace_attributes([safe_event]),
        conversation=None,
        started_at_by_span_id={},
        memory_content=memory_content,
    )


def _root_span(
    events: list[TraceEvent],
    *,
    conversation: "TraceConversationView | None",
    started_at_by_span_id: Mapping[str, datetime],
) -> OtelSpanSpec:
    trace_attributes = _trace_attributes(events)
    started_at = min(
        _span_start_time(event, started_at_by_span_id=started_at_by_span_id)
        for event in events
    )
    finished_at = max(event.created_at for event in events)
    vision_trace = _is_vision_observation_trace(events)
    diagnostic_attributes = (
        {}
        if vision_trace
        else {
            **_turn_summary_attributes(events),
            **_text_latency_attributes(events),
            **_agent_service_latency_attributes(events),
            **build_turn_diagnostic(events).langfuse_trace_metadata(),
        }
    )
    return OtelSpanSpec(
        trace_id=events[0].trace_id,
        span_id=_root_span_id(events),
        parent_span_id=_external_parent_span_id(events),
        name="vision.runtime" if vision_trace else "agent.runtime",
        start_time=started_at,
        end_time=finished_at,
        status=_root_status(events),
        attributes={
            **trace_attributes,
            "langfuse.observation.type": "span",
            "assistant_agent.canonical_event": "run",
            "assistant_agent.node_name": "runtime",
            **diagnostic_attributes,
            **_root_io_attributes(events, conversation=conversation),
        },
    )


def _external_parent_span_id(events: list[TraceEvent]) -> str | None:
    """Return the parent supplied by an enclosing experiment/task context."""

    root_span_id = _root_span_id(events)
    return next(
        (
            event.parent_span_id
            for event in events
            if _event_name(event) == "run.started"
            and event.parent_span_id
            and event.parent_span_id != root_span_id
        ),
        None,
    )


def _event_iterations(events: list[TraceEvent]) -> dict[int, int]:
    assignments: dict[int, int] = {}
    current_iteration: int | None = None
    for index, event in enumerate(events):
        explicit_iteration = _mapping_int(event.attributes, "iteration")
        if explicit_iteration is not None:
            current_iteration = explicit_iteration
        if (
            event.observation_type is not None
            and event.observation_scope == "iteration"
            and current_iteration is not None
        ):
            assignments[index] = current_iteration
    return assignments


def _iteration_spans(
    events: list[TraceEvent],
    *,
    event_iterations: dict[int, int],
    parent_span_id: str,
    trace_attributes: dict[str, Any],
    started_at_by_span_id: Mapping[str, datetime],
) -> dict[int, OtelSpanSpec]:
    grouped: dict[int, list[TraceEvent]] = {}
    for index, iteration in event_iterations.items():
        grouped.setdefault(iteration, []).append(events[index])
    spans: dict[int, OtelSpanSpec] = {}
    for iteration, iteration_events in sorted(grouped.items()):
        statuses = [_event_status(event) for event in iteration_events]
        status: Literal["ok", "error", "unset"] = (
            "error" if "error" in statuses else "ok" if "ok" in statuses else "unset"
        )
        spans[iteration] = OtelSpanSpec(
            trace_id=iteration_events[0].trace_id,
            span_id=_stable_span_id(iteration_events[0].trace_id, f"react.iteration.{iteration}"),
            parent_span_id=parent_span_id,
            name="react.iteration",
            start_time=min(
                _span_start_time(
                    event,
                    started_at_by_span_id=started_at_by_span_id,
                )
                for event in iteration_events
            ),
            end_time=max(event.created_at for event in iteration_events),
            status=status,
            attributes={
                **trace_attributes,
                "langfuse.observation.type": "span",
                "assistant_agent.canonical_event": "react.iteration",
                "assistant_agent.node_name": "assistant_loop",
                "assistant_agent.iteration": iteration,
                "langfuse.observation.input": _json_value({"iteration": iteration}),
                "langfuse.observation.output": _json_value({"status": status}),
            },
        )
    return spans


def _operation_span(
    event: TraceEvent,
    *,
    root_span_id: str,
    runtime_parent_id: str,
    iteration_parent_id: str | None,
    trace_attributes: dict[str, Any],
    conversation: "TraceConversationView | None",
    started_at_by_span_id: Mapping[str, datetime],
    memory_content: "MemoryIngestionTraceContent | None" = None,
) -> OtelSpanSpec:
    span_id = event.span_id or f"{event.run_id}:{_event_name(event)}"
    return OtelSpanSpec(
        trace_id=event.trace_id,
        span_id=span_id,
        parent_span_id=(
            event.parent_span_id
            or iteration_parent_id
            or runtime_parent_id
            or root_span_id
        ),
        name=_span_name(event),
        start_time=_span_start_time(
            event,
            started_at_by_span_id=started_at_by_span_id,
        ),
        end_time=event.created_at,
        status=_event_status(event),
        attributes={
            **trace_attributes,
            **_event_attributes(event),
            **_event_io_attributes(
                event,
                conversation=conversation,
                memory_content=memory_content,
            ),
        },
    )


def _trace_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    first = events[0]
    summary = _latest_turn_summary(events)
    user_id = _string_or_none(summary.get("user_id")) or first.user_id
    session_id = _string_or_none(summary.get("session_id")) or first.session_id
    run_id = _string_or_none(summary.get("run_id")) or first.run_id
    trace_id = _string_or_none(summary.get("trace_id")) or first.trace_id
    vision_trace = _is_vision_observation_trace(events)
    attrs: dict[str, Any] = {
        "langfuse.trace.name": (
            "vision.observation" if vision_trace else "assistant.turn"
        ),
        "langfuse.trace.metadata.assistant_trace_id": trace_id,
        "langfuse.trace.metadata.run_id": run_id,
        "assistant_agent.trace_id": trace_id,
        "assistant_agent.run_id": run_id,
        "assistant_agent.modality": (
            VISION_MODALITY if vision_trace else TEXT_MODALITY
        ),
    }
    if user_id:
        attrs["langfuse.user.id"] = user_id
    if session_id:
        attrs["langfuse.session.id"] = session_id
        attrs["assistant_agent.agent_session_id"] = session_id
        attrs["langfuse.trace.metadata.agent_session_id"] = session_id
    client_type = _string_or_none(summary.get("client_type"))
    session_scope = (
        "agent_service_connection"
        if vision_trace
        else _session_scope(client_type)
    )
    attrs["assistant_agent.session_scope"] = session_scope
    attrs["langfuse.trace.metadata.session_scope"] = session_scope
    if vision_trace:
        attrs["langfuse.trace.metadata.trace_kind"] = "vision_observation"
    return attrs


def _turn_summary_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    summary = _latest_turn_summary(events)
    attrs: dict[str, Any] = {}
    for key in (
        "run_id",
        "turn_id",
        "session_turn",
        "terminal_status",
        "response_present",
        "tool_count",
        "error_count",
        "client_type",
    ):
        value = summary.get(key)
        if _safe_scalar(value):
            attrs[f"assistant_agent.{key}"] = value
            attrs[f"langfuse.trace.metadata.{key}"] = value
    return attrs


def _text_latency_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    for event in reversed(events):
        value = _mapping_int(event.attributes, "first_text_latency_ms")
        if value is not None:
            return {"assistant_agent.first_text_latency_ms": value}
    return {}


def _agent_service_latency_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    for event in reversed(events):
        if _event_name(event) != "agent_service.turn.finished":
            continue
        summary = event.output_summary.get("turn_latency")
        if not isinstance(summary, Mapping):
            return {}
        attrs: dict[str, Any] = {}
        for key in (
            "total_ms",
            "bottleneck",
            "bottleneck_ms",
            "bottleneck_share_pct",
            "unattributed_ms",
            "ack_status",
            "first_stream_chunk_latency_ms",
        ):
            value = summary.get(key)
            if not _safe_scalar(value):
                continue
            attrs[f"assistant_agent.{key}"] = value
            attrs[f"langfuse.trace.metadata.{key}"] = value
        return attrs
    return {}


def _event_attributes(event: TraceEvent) -> dict[str, Any]:
    canonical_event = _event_name(event)
    attrs: dict[str, Any] = {
        "langfuse.observation.type": _observation_type(event),
        "langfuse.observation.level": "ERROR" if _event_status(event) == "error" else "DEFAULT",
        "assistant_agent.canonical_event": canonical_event,
        "assistant_agent.node_name": event.node_name,
    }
    if canonical_event in {"tool.finished", "tool.failed"}:
        attrs["assistant_agent.observation_kind"] = "tool_execution"
        attrs[
            "langfuse.observation.metadata.assistant_agent.observation_kind"
        ] = "tool_execution"
    if event.latency_ms is not None:
        attrs["assistant_agent.latency_ms"] = event.latency_ms
    if event.provider:
        attrs["gen_ai.provider.name"] = event.provider
    if event.model:
        attrs["gen_ai.request.model"] = event.model
        attrs["gen_ai.response.model"] = event.model
        attrs["langfuse.observation.model.name"] = event.model
    if event.tool_name:
        attrs["assistant_agent.tool_name"] = event.tool_name
        if canonical_event in {"tool.finished", "tool.failed"}:
            attrs["gen_ai.tool.name"] = event.tool_name
            attrs["gen_ai.tool.type"] = "function"
    if event.error:
        error_code = event.error.get("code")
        error_message = event.error.get("message")
        if isinstance(error_code, str) and error_code:
            attrs["assistant_agent.error_code"] = sanitize_trace_value(error_code)
        if isinstance(error_message, str) and error_message:
            attrs["langfuse.observation.status_message"] = sanitize_trace_value(error_message)
    attrs.update(_safe_attributes(event.attributes, prefix="assistant_agent"))
    attrs.update(_safe_attributes(event.output_summary, prefix="assistant_agent", allowed_keys=_ALLOWED_OUTPUT_KEYS))
    runtime_action = attrs.get("assistant_agent.runtime_action")
    if isinstance(runtime_action, str):
        attrs[
            "langfuse.observation.metadata.assistant_agent.runtime_action"
        ] = runtime_action
    for key in _VISUAL_TRACE_LINK_FIELDS:
        value = attrs.get(f"assistant_agent.{key}")
        if _safe_scalar(value):
            attrs[f"langfuse.observation.metadata.assistant_agent.{key}"] = value
    source_vision_trace_url = _source_vision_trace_url(event)
    if source_vision_trace_url is not None:
        attrs["assistant_agent.source_vision_trace_url"] = source_vision_trace_url
        attrs[
            "langfuse.observation.metadata.assistant_agent.source_vision_trace_url"
        ] = source_vision_trace_url
    usage = _usage_details(event.attributes)
    if usage:
        attrs["langfuse.observation.usage_details"] = json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
        if "input" in usage:
            attrs["gen_ai.usage.input_tokens"] = usage["input"]
        if "output" in usage:
            attrs["gen_ai.usage.output_tokens"] = usage["output"]
    return attrs


def _source_vision_trace_url(event: TraceEvent) -> str | None:
    trace_id = event.output_summary.get("source_vision_trace_id")
    if not isinstance(trace_id, str) or trace_id == event.trace_id:
        return None
    return local_langfuse_trace_url(trace_id)


def _root_io_attributes(
    events: list[TraceEvent],
    *,
    conversation: "TraceConversationView | None",
) -> dict[str, str]:
    if _is_vision_observation_trace(events):
        terminal = next(
            (
                event
                for event in reversed(events)
                if _event_name(event) == "vision.observation.summary"
            ),
            events[-1],
        )
        input_value = _json_value(
            {
                "modality": VISION_MODALITY,
                "content_exported": False,
                "media_kind": terminal.attributes.get("media_kind", "live_view"),
            }
        )
        vlm_output = _latest_vlm_output_for_events(events, conversation=conversation)
        output_payload = (
            {
                **vlm_output.normalized_result,
                "content_exported": True,
            }
            if vlm_output is not None
            else {
                "status": terminal.output_summary.get(
                    "status", terminal.status or "unknown"
                ),
                "content_exported": False,
            }
        )
        output_value = _json_value(output_payload)
        return {
            "langfuse.observation.input": input_value,
            "langfuse.observation.output": output_value,
            "langfuse.trace.input": input_value,
            "langfuse.trace.output": output_value,
        }
    summary = _latest_turn_summary(events)
    if conversation is not None:
        input_payload: dict[str, Any] = {
            "role": "user",
            "content": conversation.user.text,
            "chars": conversation.user.chars,
            "truncated": conversation.user.truncated,
        }
        delivered = conversation.delivered or conversation.assistant
        output_payload: dict[str, Any] = {
            "role": "assistant",
            "content": delivered.text,
            "chars": delivered.chars,
            "truncated": delivered.truncated,
            "terminal_status": summary.get("terminal_status", "unknown"),
        }
    else:
        input_payload = {"modality": TEXT_MODALITY, "content_exported": False}
        output_payload = {
            "terminal_status": summary.get("terminal_status", "unknown"),
            "response_present": bool(summary.get("response_present")),
            "tool_count": summary.get("tool_count", 0),
            "error_count": summary.get("error_count", 0),
        }
    input_value = _json_value(input_payload)
    output_value = _json_value(output_payload)
    return {
        "langfuse.observation.input": input_value,
        "langfuse.observation.output": output_value,
        "langfuse.trace.input": input_value,
        "langfuse.trace.output": output_value,
    }


def _event_io_attributes(
    event: TraceEvent,
    *,
    conversation: "TraceConversationView | None",
    memory_content: "MemoryIngestionTraceContent | None" = None,
) -> dict[str, str]:
    name = _event_name(event)
    input_payload: Any = {"operation": _span_name(event)}
    output_payload: Any = {
        "status": event.status or _event_status(event),
    }
    include_output = name != "llm.chat.finished"
    if event.latency_ms is not None:
        output_payload["latency_ms"] = event.latency_ms

    if name in {"assistant.output", "react.decision"}:
        input_payload = {
            "iteration": event.attributes.get("iteration"),
            "plan_status": event.attributes.get("plan_status"),
        }
        output_payload = _selected_payload(
            {**event.output_summary, **event.attributes},
            (
                "output_type",
                "decision_type",
                "tool_name",
                "reason",
                "confidence",
                "step_id",
                "plan_status",
            ),
        )
        output_payload.setdefault(
            "output_type" if name == "assistant.output" else "decision_type",
            event.status or "unknown",
        )
        if event.tool_name:
            output_payload["tool_name"] = event.tool_name
    elif name in {"tool.finished", "tool.failed"}:
        local_tool_result = _tool_result_for_event(
            conversation,
            span_id=event.span_id,
        )
        if local_tool_result is not None and _metadata_only_content(event):
            input_payload = {
                "tool_name": event.tool_name,
                **_selected_payload(
                    event.input_summary,
                    ("field_count", "media_count", "prompt_length"),
                ),
            }
            output_payload = sanitize_visual_tool_result(
                local_tool_result.result
            )
            output_payload.update(
                _selected_payload(
                    event.output_summary,
                    _VISUAL_TRACE_LINK_FIELDS,
                )
            )
        elif conversation is not None and not _metadata_only_content(event):
            input_payload = {"tool_name": event.tool_name, **event.input_summary}
            output_payload = {
                "tool_name": event.tool_name,
                **{
                    key: value
                    for key, value in event.output_summary.items()
                    if key != "model_observation"
                },
            }
        else:
            input_payload = {
                "tool_name": event.tool_name,
                **_selected_payload(
                    event.input_summary,
                    ("field_count", "media_count", "prompt_length"),
                ),
            }
            output_payload = {
                "tool_name": event.tool_name,
                **_selected_payload(
                    event.output_summary,
                    (
                        "success",
                        "output_ref",
                        "error_code",
                        "item_count",
                        "result_count",
                        "artifact_id",
                        "artifact_ref",
                        *_VISUAL_TRACE_LINK_FIELDS,
                    ),
                ),
            }
        source_vision_trace_url = _source_vision_trace_url(event)
        if source_vision_trace_url is not None:
            output_payload["source_vision_trace_url"] = source_vision_trace_url
    elif name == "tool.observation":
        tool_observation = _tool_observation_for_event(
            conversation,
            observation_index=_mapping_int(
                event.attributes, "observation_index"
            ),
            tool_name=event.tool_name,
        )
        input_payload = {
            "tool_name": event.tool_name,
            "runtime_tool_call_id": (
                tool_observation.runtime_tool_call_id
                if tool_observation is not None
                else event.attributes.get("tool_call_id")
            ),
            "provider_tool_call_id": (
                tool_observation.provider_tool_call_id
                if tool_observation is not None
                else None
            ),
            "source_tool_span_id": event.attributes.get("source_tool_span_id"),
        }
        if tool_observation is not None:
            observation_payload = {
                key: value
                for key, value in tool_observation.observation.items()
                if key != "_provider_tool_call_id"
            }
            output_payload = (
                sanitize_visual_trace_content(observation_payload)
                if _metadata_only_content(event)
                else observation_payload
            )
        else:
            output_payload = {
                "tool_name": event.tool_name,
                **_selected_payload(
                    {**event.output_summary, **event.attributes},
                    ("summary", "output_ref"),
                ),
            }
    elif name in {
        "memory.session_recall.finished",
        "memory.ingestion.finished",
    }:
        if (
            name == "memory.ingestion.finished"
            and memory_content is not None
            and memory_content.user_text
            and memory_content.assistant_text
        ):
            input_payload = {
                "source_turn": memory_content.source_turn,
                "messages": [
                    {"role": "user", "content": memory_content.user_text},
                    {"role": "assistant", "content": memory_content.assistant_text},
                ],
            }
        output_payload.update(
            _selected_payload(
                {**event.output_summary, **event.attributes},
                (
                    "memory_count",
                    "change_counts",
                    "memory_ids",
                    "source_turn",
                    "content_capture_status",
                    "error_codes",
                    "pending_count",
                ),
            )
        )
        if name == "memory.ingestion.finished":
            output_payload["content_exported"] = memory_content is not None
            if memory_content is not None:
                output_payload["changes"] = [
                    change.model_dump(mode="json")
                    for change in memory_content.changes
                ]
    elif name == "context.build.finished":
        context_report = event.output_summary.get("context_report_v2")
        if not isinstance(context_report, Mapping):
            legacy_report = event.output_summary.get("context_report_v1")
            context_report = (
                context_report_trace_payload(
                    context_report_v2_from_v1(dict(legacy_report))
                )
                if isinstance(legacy_report, Mapping)
                else None
            )
        output_payload = {
            "output_kind": event.output_summary.get(
                "output_kind",
                "prompt_safe_context_compilation_report",
            ),
            "status": event.status or _event_status(event),
            "latency_ms": event.latency_ms,
            "iteration": event.attributes.get("iteration"),
            "build_reason": event.attributes.get("build_reason"),
            "compiled_request_shape": _safe_payload_value(
                event.output_summary.get("compiled_request_shape")
            ),
            "compiled_request_ref": _safe_payload_value(
                event.output_summary.get("compiled_request_ref")
                or event.output_summary.get("compiled_request_content")
            ),
            "context_report_v2": _safe_payload_value(
                context_report
            ),
        }
    elif name == "llm.chat.finished":
        llm_input = _llm_input_for_event(
            conversation,
            span_id=event.span_id,
            iteration=_mapping_int(event.attributes, "iteration"),
        )
        input_payload = (
            _llm_provider_input(llm_input.request)
            if llm_input is not None
            else _pretty_json_text(
                _selected_payload(event.attributes, ("iteration", "input_tokens"))
            )
        )
        llm_output = _llm_output_for_event(conversation, span_id=event.span_id)
        if llm_output is not None:
            output_payload = _llm_provider_output_preview(llm_output)
            include_output = True
    elif name == "vlm.infer.finished":
        input_payload = {
            "operation": "vlm.infer",
            **_selected_payload(
                event.attributes,
                (
                    "media_kind",
                    "media_count",
                    "prompt_version",
                    "source",
                    "frame_sequence",
                ),
            ),
            "content_exported": False,
        }
        vlm_output = _vlm_output_for_event(conversation, span_id=event.span_id)
        if vlm_output is not None:
            output_payload = {
                **vlm_output.normalized_result,
                "content_exported": True,
            }
            include_output = True
    elif name == "response.final" and conversation is not None:
        output_payload = {
            "role": "assistant",
            "content": conversation.assistant.text,
            "chars": conversation.assistant.chars,
            "truncated": conversation.assistant.truncated,
        }
    elif name == "response.delivered" and conversation is not None:
        delivered = conversation.delivered or conversation.assistant
        output_payload = {
            "role": "assistant",
            "content": delivered.text,
            "chars": delivered.chars,
            "truncated": delivered.truncated,
            "source": event.attributes.get("source"),
        }

    attributes = {"langfuse.observation.input": _json_value(_drop_none_if_mapping(input_payload))}
    if name == "memory.ingestion.finished":
        semantic_evidence = (
            "available"
            if memory_content is not None
            and memory_content.user_text
            and memory_content.assistant_text
            else "unavailable"
        )
        attributes["assistant_agent.memory_semantic_evidence"] = semantic_evidence
        attributes[
            "langfuse.observation.metadata.assistant_agent.memory_semantic_evidence"
        ] = semantic_evidence
    if include_output:
        serialized_output = (
            output_payload
            if name == "llm.chat.finished"
            else _drop_none_if_mapping(output_payload)
        )
        attributes["langfuse.observation.output"] = _json_value(
            serialized_output
        )
    return attributes


def _llm_provider_input(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the adapter-captured SDK request without presentation rewrites."""

    return dict(request)


def _llm_provider_output_preview(llm_output: "TraceLlmOutput") -> dict[str, Any]:
    """Return one Provider reply as an OpenAI-compatible assistant message."""

    protocol = llm_output.provider_protocol_response
    if isinstance(protocol, Mapping):
        tool_calls = [
            {
                "id": item.get("id"),
                "type": item.get("type") or "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": item.get("arguments_raw", ""),
                },
            }
            for item in protocol.get("tool_calls", [])
            if isinstance(item, Mapping)
        ]
        return _provider_reply_message(
            content=str(protocol.get("content") or ""),
            tool_calls=tool_calls,
            refusal=protocol.get("refusal"),
        )

    normalized = llm_output.normalized_result
    tool_calls = []
    for item in normalized.get("tool_calls", []):
        if not isinstance(item, Mapping):
            continue
        raw = item.get("raw")
        if isinstance(raw, Mapping) and raw:
            tool_calls.append(dict(raw))
            continue
        tool_calls.append(
            {
                "id": item.get("id"),
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": json.dumps(
                        item.get("arguments", {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
        )
    return _provider_reply_message(
        content=str(normalized.get("response_text") or ""),
        tool_calls=tool_calls,
        refusal=normalized.get("refusal"),
        errors=normalized.get("errors", []),
    )


def _provider_reply_message(
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
    refusal: Any,
    errors: Any = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if refusal:
        message["refusal"] = refusal
    if errors:
        message["errors"] = errors
    return message


def _pretty_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _drop_none_if_mapping(value: Any) -> Any:
    return _drop_none(value) if isinstance(value, Mapping) else value


def _selected_payload(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _safe_payload_value(source[key])
        for key in keys
        if key in source and source[key] is not None
    }


def _llm_input_for_event(
    conversation: "TraceConversationView | None",
    *,
    span_id: str | None,
    iteration: int | None,
) -> "TraceLlmInput | None":
    if conversation is None:
        return None
    if span_id is not None:
        matched = next(
            (item for item in conversation.llm_inputs if item.span_id == span_id),
            None,
        )
        if matched is not None:
            return matched
    if iteration is None:
        return None
    return next(
        (item for item in conversation.llm_inputs if item.iteration == iteration),
        None,
    )


def _llm_output_for_event(
    conversation: "TraceConversationView | None",
    *,
    span_id: str | None,
) -> "TraceLlmOutput | None":
    if conversation is None or span_id is None:
        return None
    return next(
        (item for item in conversation.llm_outputs if item.span_id == span_id),
        None,
    )


def _vlm_output_for_event(
    conversation: "TraceConversationView | None",
    *,
    span_id: str | None,
) -> "TraceVlmOutput | None":
    if conversation is None or span_id is None:
        return None
    return next(
        (item for item in reversed(conversation.vlm_outputs) if item.span_id == span_id),
        None,
    )


def _latest_vlm_output_for_events(
    events: list[TraceEvent],
    *,
    conversation: "TraceConversationView | None",
) -> "TraceVlmOutput | None":
    for event in reversed(events):
        if (
            _event_name(event) == "vlm.infer.finished"
            and _event_status(event) != "error"
        ):
            matched = _vlm_output_for_event(conversation, span_id=event.span_id)
            if matched is not None:
                return matched
    return None


def _tool_observation_for_event(
    conversation: "TraceConversationView | None",
    *,
    observation_index: int | None,
    tool_name: str | None,
) -> "TraceToolObservation | None":
    if conversation is None:
        return None
    if observation_index is not None:
        matched = next(
            (
                item
                for item in conversation.tool_observations
                if item.observation_index == observation_index
            ),
            None,
        )
        if matched is not None:
            return matched
    if tool_name is None:
        return None
    return next(
        (
            item
            for item in conversation.tool_observations
            if item.tool_name == tool_name
        ),
        None,
    )


def _tool_result_for_event(
    conversation: "TraceConversationView | None",
    *,
    span_id: str | None,
) -> "TraceToolResult | None":
    if conversation is None or span_id is None:
        return None
    return next(
        (
            item
            for item in getattr(conversation, "tool_results", [])
            if item.span_id == span_id
        ),
        None,
    )


def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_trace_value(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:20]]
    if isinstance(value, Mapping):
        return {
            str(key): _safe_payload_value(item)
            for key, item in list(value.items())[:30]
        }
    return sanitize_trace_value(str(value))


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _json_value(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_attributes(
    source: Mapping[str, Any],
    *,
    prefix: str,
    allowed_keys: frozenset[str] = _ALLOWED_ATTRIBUTE_KEYS,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, value in source.items():
        if key not in allowed_keys or not _text_key_allowed(key) or not _safe_scalar(value):
            continue
        if isinstance(value, str):
            value = sanitize_trace_value(value)
        attrs[f"{prefix}.{key}"] = value
    return attrs


def _usage_details(source: Mapping[str, Any]) -> dict[str, int]:
    nested = source.get("usage")
    usage_source = nested if isinstance(nested, Mapping) else source
    usage: dict[str, int] = {}
    input_tokens = _first_mapping_int(usage_source, ("input_tokens", "prompt_tokens"))
    output_tokens = _first_mapping_int(usage_source, ("output_tokens", "completion_tokens"))
    total_tokens = _first_mapping_int(usage_source, ("total_tokens", "token_count"))
    if input_tokens is not None:
        usage["input"] = input_tokens
    if output_tokens is not None:
        usage["output"] = output_tokens
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    if total_tokens is not None:
        usage["total"] = total_tokens
    return usage


def _first_mapping_int(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _mapping_int(source, key)
        if value is not None:
            return value
    return None


def _latest_turn_summary(events: list[TraceEvent]) -> dict[str, Any]:
    return latest_turn_summary_from_events(events) or {}


def _root_span_id(events: list[TraceEvent]) -> str:
    root_name = (
        "vision.runtime"
        if _is_vision_observation_trace(events)
        else "agent.runtime"
    )
    return _stable_span_id(events[0].trace_id, root_name)


def _is_vision_observation_trace(events: Iterable[TraceEvent]) -> bool:
    return any(
        _event_name(event) == "vision.observation.summary"
        and event.attributes.get("trace_kind") == "vision_observation"
        for event in events
    )


def _metadata_only_content(event: TraceEvent) -> bool:
    return any(
        source.get("content_export_policy") == "metadata_only"
        for source in (
            event.attributes,
            event.input_summary,
            event.output_summary,
        )
    )


def _stable_span_id(trace_id: str, name: str) -> str:
    return sha256(f"{trace_id}:{name}".encode("utf-8")).digest()[:8].hex()


def _root_status(events: list[TraceEvent]) -> Literal["ok", "error", "unset"]:
    for event in reversed(events):
        status = _event_status(event)
        if status != "unset":
            return status
    return "unset"


def _event_status(event: TraceEvent) -> Literal["ok", "error", "unset"]:
    status = (event.status or "").lower()
    canonical_event = _event_name(event)
    if event.error or event.error_code or status in {"failed", "error"} or canonical_event.endswith(".failed"):
        return "error"
    if status in {"completed", "success", "sent", "acked", "ok"}:
        return "ok"
    if canonical_event.endswith((".finished", ".completed", ".final")):
        return "ok"
    return "unset"


def _span_name(event: TraceEvent) -> str:
    if event.observation_name:
        return event.observation_name
    canonical_event = _event_name(event)
    if canonical_event in {"tool.finished", "tool.failed"}:
        return "tool.execute"
    if canonical_event.endswith(".finished"):
        return canonical_event.removesuffix(".finished")
    if canonical_event.endswith(".failed"):
        return canonical_event.removesuffix(".failed")
    if canonical_event == "response.final":
        return "response.final"
    if canonical_event == "response.delivered":
        return "response.delivered"
    return canonical_event


def _observation_type(event: TraceEvent) -> str:
    return event.observation_type or "span"


def _started_at_by_span_id(events: Iterable[TraceEvent]) -> dict[str, datetime]:
    started_at: dict[str, datetime] = {}
    for event in events:
        if not event.span_id or not _event_name(event).endswith(".started"):
            continue
        previous = started_at.get(event.span_id)
        if previous is None or event.created_at < previous:
            started_at[event.span_id] = event.created_at
    return started_at


def _span_start_time(
    event: TraceEvent,
    *,
    started_at_by_span_id: Mapping[str, datetime],
) -> datetime:
    if event.span_id:
        explicit_started_at = started_at_by_span_id.get(event.span_id)
        if explicit_started_at is not None:
            return explicit_started_at
    latency_ms = event.latency_ms
    if _event_name(event) == "llm.chat.finished":
        latency_ms = _mapping_int(event.attributes, "wall_latency_ms") or latency_ms
    if latency_ms is None:
        return event.created_at
    return event.created_at - timedelta(milliseconds=latency_ms)


def _event_name(event: TraceEvent) -> str:
    return str(event.canonical_event or event.event_type or event.node_name)


def _mapping_int(source: Mapping[str, Any], key: str) -> int | None:
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_key_allowed(key: str) -> bool:
    lowered = key.lower()
    return not any(token in lowered for token in _VOICE_ATTRIBUTE_TOKENS)


def _safe_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool) and value is not None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _session_scope(client_type: str | None) -> str:
    if client_type in {"media_agent", "run_client"}:
        return "agent_service_connection"
    return "logical_conversation"
