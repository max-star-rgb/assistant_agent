"""Content-safe canonical observability for VLM inference calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
    new_span_id,
)
from assistant_agent.observability.trace_content_policy import (
    local_trace_content_enabled,
)


VISION_INFERENCE_OBSERVATION_NAME = "vlm.infer"
VISION_INFERENCE_PROMPT_VERSION = "vision-understanding-v1"
_ResultT = TypeVar("_ResultT")
_VLM_OUTPUT_FIELDS = (
    "summary",
    "scene",
    "objects",
    "people",
    "actions",
    "events",
    "changes",
    "uncertainties",
    "text_in_media",
    "text_in_video",
    "products",
    "brands",
    "colors",
    "materials",
    "style_tags",
    "timestamps",
    "confidence",
    "provider",
    "model",
    "latency_ms",
)
_MAX_VLM_CONTENT_TEXT_CHARS = 4_000
_MAX_VLM_CONTENT_ITEMS = 20
_VLM_INPUT_FIELDS = (
    "mode",
    "prompt_version",
    "resolved_instructions",
    "query",
    "media_kind",
    "frame_sequence",
    "frame_count",
    "history_frame_count",
    "memory_context_present",
)
_BLOCKED_VLM_CONTENT_KEYS = frozenset(
    {
        "evidence_ref",
        "frame_ref",
        "frame_refs",
        "image_id",
        "image_ids",
        "media_ref",
        "media_refs",
        "output_ref",
        "path",
        "provider_raw_response",
        "raw_provider_payload",
        "uri",
        "video_id",
        "video_ids",
    }
)


class VisionInferenceTraceContext(Protocol):
    """Minimal correlation contract accepted from any governed caller."""

    run_id: str | None
    trace_id: str | None
    trace_store: TraceStore | None
    parent_span_id: str | None
    user_id: str | None
    session_id: str | None


class VisionInferenceTraceLink(BaseModel):
    """Prompt-safe identity of one VLM generation."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    run_id: str
    span_id: str


def observe_vision_inference(
    call: Callable[[], _ResultT],
    *,
    context: VisionInferenceTraceContext,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    frame_sequence: int | None = None,
    query_provided: bool | None = None,
    prompt_version: str = VISION_INFERENCE_PROMPT_VERSION,
    local_input_content: Mapping[str, Any] | None = None,
    trace_link_callback: Callable[[VisionInferenceTraceLink], None] | None = None,
) -> _ResultT:
    """Run one VLM call and emit a redacted generation when tracing is active."""

    trace_store = context.trace_store
    trace_id = context.trace_id
    run_id = context.run_id
    if trace_store is None or not trace_id or not run_id:
        return call()

    span_id = new_span_id()
    _notify_trace_link_fail_open(
        trace_link_callback,
        VisionInferenceTraceLink(
            trace_id=trace_id,
            run_id=run_id,
            span_id=span_id,
        ),
    )
    common = {
        "capability": capability,
        "source": source,
        "media_kind": media_kind,
        "media_count": max(0, int(media_count)),
        "prompt_version": prompt_version,
        "model_role": "vlm",
        **(
            {"frame_sequence": frame_sequence}
            if isinstance(frame_sequence, int)
            and not isinstance(frame_sequence, bool)
            and frame_sequence >= 0
            else {}
        ),
        **(
            {"query_provided": query_provided}
            if isinstance(query_provided, bool)
            else {}
        ),
    }
    _append_fail_open(
        trace_store,
        trace_id=trace_id,
        run_id=run_id,
        user_id=context.user_id,
        session_id=context.session_id,
        canonical_event="vlm.infer.started",
        observation_name=VISION_INFERENCE_OBSERVATION_NAME,
        observation_scope="iteration",
        node_name="vision_understanding",
        status="started",
        span_id=span_id,
        parent_span_id=context.parent_span_id,
        attributes=common,
        input_summary={
            "media_kind": media_kind,
            "media_count": max(0, int(media_count)),
            "prompt_version": prompt_version,
            "source": source,
            **(
                {"frame_sequence": frame_sequence}
                if isinstance(frame_sequence, int)
                and not isinstance(frame_sequence, bool)
                and frame_sequence >= 0
                else {}
            ),
            **(
                {"query_provided": query_provided}
                if isinstance(query_provided, bool)
                else {}
            ),
        },
    )
    _append_vlm_input_fail_open(
        context=context,
        span_id=span_id,
        input_content=local_input_content,
    )
    started_at = perf_counter()
    try:
        result = call()
    except Exception as exc:
        latency_ms = max(0, int((perf_counter() - started_at) * 1000))
        _append_fail_open(
            trace_store,
            trace_id=trace_id,
            run_id=run_id,
            user_id=context.user_id,
            session_id=context.session_id,
            canonical_event="vlm.infer.finished",
            observation_type="generation",
            observation_name=VISION_INFERENCE_OBSERVATION_NAME,
            observation_scope="iteration",
            node_name="vision_understanding",
            status="failed",
            latency_ms=latency_ms,
            span_id=span_id,
            parent_span_id=context.parent_span_id,
            attributes={**common, "wall_latency_ms": latency_ms},
            output_summary={"status": "failed"},
            error={
                "code": _error_code(exc),
                "message": "VLM inference failed.",
            },
        )
        raise

    latency_ms = max(0, int((perf_counter() - started_at) * 1000))
    provider = _safe_text(getattr(result, "provider", None))
    model = _safe_text(getattr(result, "model", None))
    errors = getattr(result, "errors", None)
    failed = isinstance(errors, list) and bool(errors)
    _append_fail_open(
        trace_store,
        trace_id=trace_id,
        run_id=run_id,
        user_id=context.user_id,
        session_id=context.session_id,
        canonical_event="vlm.infer.finished",
        observation_type="generation",
        observation_name=VISION_INFERENCE_OBSERVATION_NAME,
        observation_scope="iteration",
        node_name="vision_understanding",
        status="failed" if failed else "succeeded",
        provider=provider,
        model=model,
        latency_ms=_result_latency_ms(result, latency_ms),
        span_id=span_id,
        parent_span_id=context.parent_span_id,
        attributes={**common, "wall_latency_ms": latency_ms},
        output_summary={
            "status": "failed" if failed else "succeeded",
            "error_count": len(errors) if isinstance(errors, list) else 0,
        },
        error=_result_error(errors),
    )
    _append_vlm_content_fail_open(
        context=context,
        span_id=span_id,
        result=result,
        provider=provider,
        model=model,
    )
    return result


def _append_fail_open(trace_store: TraceStore, **kwargs: Any) -> None:
    try:
        append_observability_event(trace_store, **kwargs)
    except Exception:
        return


def _notify_trace_link_fail_open(
    callback: Callable[[VisionInferenceTraceLink], None] | None,
    link: VisionInferenceTraceLink,
) -> None:
    if callback is None:
        return
    try:
        callback(link)
    except Exception:
        return


def _append_vlm_content_fail_open(
    *,
    context: VisionInferenceTraceContext,
    span_id: str,
    result: object,
    provider: str | None,
    model: str | None,
) -> None:
    if (
        not local_trace_content_enabled()
        or not context.user_id
        or not context.session_id
        or not context.trace_id
    ):
        return
    try:
        from assistant_agent.observability.trace_conversation import TraceVlmOutput

        _trace_content_store().append_vlm_output(
            user_id=context.user_id,
            session_id=context.session_id,
            trace_id=context.trace_id,
            vlm_output=TraceVlmOutput(
                span_id=span_id,
                provider=provider,
                model=model,
                normalized_result=_normalized_vlm_output(result),
            ),
        )
    except Exception:
        return


def _append_vlm_input_fail_open(
    *,
    context: VisionInferenceTraceContext,
    span_id: str,
    input_content: Mapping[str, Any] | None,
) -> None:
    if (
        not local_trace_content_enabled()
        or not input_content
        or not context.user_id
        or not context.session_id
        or not context.trace_id
    ):
        return
    try:
        from assistant_agent.observability.trace_conversation import TraceVlmInput

        normalized_input = {
            field: _safe_vlm_content_value(input_content[field])
            for field in _VLM_INPUT_FIELDS
            if input_content.get(field) not in (None, "", [], {})
            or isinstance(input_content.get(field), bool | int)
        }
        _trace_content_store().append_vlm_input(
            user_id=context.user_id,
            session_id=context.session_id,
            trace_id=context.trace_id,
            vlm_input=TraceVlmInput(
                span_id=span_id,
                normalized_input=normalized_input,
            ),
        )
    except Exception:
        return


def _trace_content_store():
    from assistant_agent.observability.trace_conversation import (
        get_default_trace_conversation_store,
    )

    return get_default_trace_conversation_store()


def _normalized_vlm_output(result: object) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in _VLM_OUTPUT_FIELDS:
        value = getattr(result, field, None)
        if value not in (None, "", [], {}):
            payload[field] = _safe_vlm_content_value(value)
    return payload


def _safe_vlm_content_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_VLM_CONTENT_TEXT_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in list(value.items())[:_MAX_VLM_CONTENT_ITEMS]:
            normalized_key = str(key).strip().lower()
            if _blocked_vlm_content_key(normalized_key):
                continue
            result[str(key)[:120]] = _safe_vlm_content_value(nested)
        return result
    if isinstance(value, list | tuple):
        return [
            _safe_vlm_content_value(item)
            for item in value[:_MAX_VLM_CONTENT_ITEMS]
        ]
    return str(value)[:_MAX_VLM_CONTENT_TEXT_CHARS]


def _blocked_vlm_content_key(key: str) -> bool:
    return key in _BLOCKED_VLM_CONTENT_KEYS or key.endswith(
        ("_bytes", "_path", "_payload", "_ref", "_refs", "_uri")
    )


def _result_latency_ms(result: object, fallback: int) -> int:
    value = getattr(result, "latency_ms", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def _result_error(errors: object) -> dict[str, str] | None:
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    first = errors[0]
    return {
        "code": _safe_text(first.get("code")) or "provider_call_failed",
        "message": "VLM inference failed.",
    }


def _error_code(exc: Exception) -> str:
    value = getattr(exc, "code", None)
    return _safe_text(value) or "provider_call_failed"


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
