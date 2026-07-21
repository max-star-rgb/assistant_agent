"""Prompt-safe OpenTelemetry span mapping for text assistant traces."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.services.trace_store import TraceEvent, redact_trace_event, sanitize_trace_value
from assistant_agent.services.turn_evaluator import build_turn_diagnostic
from assistant_agent.services.turn_summary import (
    ASSISTANT_TURN_SUMMARY_EVENT,
    ASSISTANT_TURN_SUMMARY_KEY,
    ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from assistant_agent.services.trace_conversation import TraceConversationView


OTEL_SPAN_SPEC_SCHEMA_VERSION = "assistant_agent_text_otel_span_spec_v1"
TEXT_MODALITY = "text"

_SPAN_EVENTS = frozenset(
    {
        "memory.load.finished",
        "context.build.finished",
        "llm.chat.finished",
        "react.decision",
        "action.validation.finished",
        "tool.finished",
        "tool.failed",
        "tool.observation",
        "loop_guard.triggered",
        "response.final",
        "memory.save.finished",
        "agent_service.turn.finished",
    }
)
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
        "assistant_run_id",
        "budget_ratio",
        "client_type",
        "context_usage_ratio",
        "decision_type",
        "error_count",
        "first_text_latency_ms",
        "gateway_run_id",
        "input_tokens",
        "iteration",
        "output_tokens",
        "provider_latency_ms",
        "response_present",
        "result_run_id",
        "runtime_call_latency_ms",
        "session_turn",
        "terminal_status",
        "tool_call_id",
        "tool_count",
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
    }
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
    """Return Langfuse's deterministic W3C trace id for an external trace id."""

    return sha256(seed.encode("utf-8")).digest()[:16].hex()


def build_text_otel_span_specs(
    events: Iterable[TraceEvent],
    *,
    conversation: "TraceConversationView | None" = None,
) -> list[OtelSpanSpec]:
    """Map redacted text-run trace events into dependency-free OTel span specs."""

    safe_events = [redact_trace_event(event) for event in events]
    if not safe_events:
        return []
    root_span = _root_span(safe_events, conversation=conversation)
    trace_attributes = _trace_attributes(safe_events)
    spans = [root_span]
    for event in safe_events:
        canonical_event = _event_name(event)
        if canonical_event not in _SPAN_EVENTS:
            continue
        if event.span_id == root_span.span_id:
            continue
        spans.append(
            _operation_span(
                event,
                root_span_id=root_span.span_id,
                trace_attributes=trace_attributes,
                conversation=conversation,
            )
        )
    return spans


def _root_span(
    events: list[TraceEvent],
    *,
    conversation: "TraceConversationView | None",
) -> OtelSpanSpec:
    trace_attributes = _trace_attributes(events)
    started_at = min(_span_start_time(event) for event in events)
    finished_at = max(event.created_at for event in events)
    return OtelSpanSpec(
        trace_id=events[0].trace_id,
        span_id=_root_span_id(events),
        name="assistant.turn",
        start_time=started_at,
        end_time=finished_at,
        status=_root_status(events),
        attributes={
            **trace_attributes,
            "langfuse.observation.type": "span",
            **_turn_summary_attributes(events),
            **_text_latency_attributes(events),
            **build_turn_diagnostic(events).langfuse_trace_metadata(),
            **_root_io_attributes(events, conversation=conversation),
        },
    )


def _operation_span(
    event: TraceEvent,
    *,
    root_span_id: str,
    trace_attributes: dict[str, Any],
    conversation: "TraceConversationView | None",
) -> OtelSpanSpec:
    span_id = event.span_id or f"{event.run_id}:{_event_name(event)}"
    return OtelSpanSpec(
        trace_id=event.trace_id,
        span_id=span_id,
        parent_span_id=event.parent_span_id or root_span_id,
        name=_span_name(event),
        start_time=_span_start_time(event),
        end_time=event.created_at,
        status=_event_status(event),
        attributes={
            **trace_attributes,
            **_event_attributes(event),
            **_event_io_attributes(event, conversation=conversation),
        },
    )


def _trace_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    first = events[0]
    summary = _latest_turn_summary(events)
    user_id = _string_or_none(summary.get("user_id")) or first.user_id
    session_id = _string_or_none(summary.get("session_id")) or first.session_id
    run_id = _string_or_none(summary.get("assistant_run_id")) or first.run_id
    trace_id = _string_or_none(summary.get("trace_id")) or first.trace_id
    attrs: dict[str, Any] = {
        "langfuse.trace.name": "assistant.turn",
        "langfuse.trace.metadata.assistant_trace_id": trace_id,
        "langfuse.trace.metadata.assistant_run_id": run_id,
        "assistant_agent.trace_id": trace_id,
        "assistant_agent.run_id": run_id,
        "assistant_agent.modality": TEXT_MODALITY,
    }
    if user_id:
        attrs["langfuse.user.id"] = user_id
    if session_id:
        attrs["langfuse.session.id"] = session_id
    return attrs


def _turn_summary_attributes(events: list[TraceEvent]) -> dict[str, Any]:
    summary = _latest_turn_summary(events)
    attrs: dict[str, Any] = {}
    for key in (
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


def _event_attributes(event: TraceEvent) -> dict[str, Any]:
    canonical_event = _event_name(event)
    attrs: dict[str, Any] = {
        "langfuse.observation.type": _observation_type(event),
        "langfuse.observation.level": "ERROR" if _event_status(event) == "error" else "DEFAULT",
        "assistant_agent.canonical_event": canonical_event,
        "assistant_agent.node_name": event.node_name,
    }
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
    attrs.update(_safe_attributes(event.attributes, prefix="assistant_agent"))
    attrs.update(_safe_attributes(event.output_summary, prefix="assistant_agent", allowed_keys=_ALLOWED_OUTPUT_KEYS))
    usage = _usage_details(event.attributes)
    if usage:
        attrs["langfuse.observation.usage_details"] = json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
        if "input" in usage:
            attrs["gen_ai.usage.input_tokens"] = usage["input"]
        if "output" in usage:
            attrs["gen_ai.usage.output_tokens"] = usage["output"]
    return attrs


def _root_io_attributes(
    events: list[TraceEvent],
    *,
    conversation: "TraceConversationView | None",
) -> dict[str, str]:
    summary = _latest_turn_summary(events)
    if conversation is not None:
        input_payload: dict[str, Any] = {
            "role": "user",
            "content": sanitize_trace_value(conversation.user.text),
            "chars": conversation.user.chars,
            "truncated": conversation.user.truncated,
        }
        output_payload: dict[str, Any] = {
            "role": "assistant",
            "content": sanitize_trace_value(conversation.assistant.text),
            "chars": conversation.assistant.chars,
            "truncated": conversation.assistant.truncated,
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
) -> dict[str, str]:
    name = _event_name(event)
    input_payload: dict[str, Any] = {"operation": _span_name(event)}
    output_payload: dict[str, Any] = {
        "status": event.status or _event_status(event),
    }
    if event.latency_ms is not None:
        output_payload["latency_ms"] = event.latency_ms

    if name == "react.decision":
        input_payload = {
            "iteration": event.attributes.get("iteration"),
            "plan_status": event.attributes.get("plan_status"),
        }
        output_payload = _selected_payload(
            {**event.output_summary, **event.attributes},
            ("decision_type", "tool_name", "reason", "confidence", "step_id", "plan_status"),
        )
        output_payload.setdefault("decision_type", event.status or "unknown")
        if event.tool_name:
            output_payload["tool_name"] = event.tool_name
    elif name in {"tool.finished", "tool.failed"}:
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
                ),
            ),
        }
    elif name == "tool.observation":
        input_payload = {"tool_name": event.tool_name}
        output_payload = {
            "tool_name": event.tool_name,
            **_selected_payload(
                {**event.output_summary, **event.attributes},
                ("summary", "output_ref", "next_step_hint"),
            ),
        }
    elif name == "llm.chat.finished":
        input_payload = _selected_payload(
            event.attributes,
            ("iteration", "input_tokens"),
        )
        input_payload.update({"provider": event.provider, "model": event.model})
        output_payload = _selected_payload(
            event.attributes,
            ("finish_reason", "output_tokens", "provider_latency_ms", "wall_latency_ms"),
        )
        output_payload.update({"status": event.status, "provider": event.provider, "model": event.model})
    elif name == "response.final" and conversation is not None:
        output_payload = {
            "role": "assistant",
            "content": sanitize_trace_value(conversation.assistant.text),
            "chars": conversation.assistant.chars,
            "truncated": conversation.assistant.truncated,
        }

    return {
        "langfuse.observation.input": _json_value(_drop_none(input_payload)),
        "langfuse.observation.output": _json_value(_drop_none(output_payload)),
    }


def _selected_payload(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _safe_payload_value(source[key])
        for key in keys
        if key in source and source[key] is not None
    }


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


def _json_value(payload: Mapping[str, Any]) -> str:
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
    usage: dict[str, int] = {}
    input_tokens = _mapping_int(source, "input_tokens")
    output_tokens = _mapping_int(source, "output_tokens")
    if input_tokens is not None:
        usage["input"] = input_tokens
    if output_tokens is not None:
        usage["output"] = output_tokens
    return usage


def _latest_turn_summary(events: list[TraceEvent]) -> dict[str, Any]:
    for event in reversed(events):
        if _event_name(event) != ASSISTANT_TURN_SUMMARY_EVENT:
            continue
        summary = event.output_summary.get(ASSISTANT_TURN_SUMMARY_KEY)
        if (
            isinstance(summary, dict)
            and summary.get("schema_version") == ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION
        ):
            return summary
    return {}


def _root_span_id(events: list[TraceEvent]) -> str:
    for event in events:
        if _event_name(event) == "run.started" and event.span_id:
            return event.span_id
    for event in events:
        if event.span_id:
            return event.span_id
    return f"{events[0].run_id}:root"


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
    canonical_event = _event_name(event)
    if canonical_event in {"tool.finished", "tool.failed"}:
        return "tool.execute"
    if canonical_event.endswith(".finished"):
        return canonical_event.removesuffix(".finished")
    if canonical_event.endswith(".failed"):
        return canonical_event.removesuffix(".failed")
    if canonical_event == "response.final":
        return "response.final"
    return canonical_event


def _observation_type(event: TraceEvent) -> str:
    if _event_name(event) == "llm.chat.finished" or event.model:
        return "generation"
    if _event_name(event) in {"react.decision", "tool.observation", "loop_guard.triggered"}:
        return "event"
    return "span"


def _span_start_time(event: TraceEvent) -> datetime:
    if event.latency_ms is None:
        return event.created_at
    return event.created_at - timedelta(milliseconds=event.latency_ms)


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
