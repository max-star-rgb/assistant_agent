"""Content-safe canonical observability for VLM inference calls."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, Protocol, TypeVar

from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
    new_span_id,
)


VISION_INFERENCE_OBSERVATION_NAME = "vlm.infer"
VISION_INFERENCE_PROMPT_VERSION = "vision-understanding-v1"
_ResultT = TypeVar("_ResultT")


class VisionInferenceTraceContext(Protocol):
    """Minimal correlation contract accepted from any governed caller."""

    run_id: str | None
    trace_id: str | None
    trace_store: TraceStore | None
    parent_span_id: str | None
    user_id: str | None
    session_id: str | None


def observe_vision_inference(
    call: Callable[[], _ResultT],
    *,
    context: VisionInferenceTraceContext,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    prompt_version: str = VISION_INFERENCE_PROMPT_VERSION,
) -> _ResultT:
    """Run one VLM call and emit a redacted generation when tracing is active."""

    trace_store = context.trace_store
    trace_id = context.trace_id
    run_id = context.run_id
    if trace_store is None or not trace_id or not run_id:
        return call()

    span_id = new_span_id()
    common = {
        "capability": capability,
        "source": source,
        "media_kind": media_kind,
        "media_count": max(0, int(media_count)),
        "prompt_version": prompt_version,
        "model_role": "vlm",
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
        },
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
    return result


def _append_fail_open(trace_store: TraceStore, **kwargs: Any) -> None:
    try:
        append_observability_event(trace_store, **kwargs)
    except Exception:
        return


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
