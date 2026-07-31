"""Stable timing contracts for exported observability spans."""

from datetime import datetime, timedelta, timezone

from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_store import TraceEvent


def test_started_events_define_context_and_llm_span_start_times() -> None:
    """Preserve causal order when integer latency reconstruction would reverse it."""

    base = datetime(2026, 7, 28, 7, 27, 20, tzinfo=timezone.utc)
    context_started_at = base
    context_finished_at = base + timedelta(microseconds=500)
    llm_started_at = base + timedelta(microseconds=700)
    llm_finished_at = base + timedelta(seconds=2, microseconds=100)
    events = [
        _event(
            canonical_event="context.build.started",
            span_id="context-span",
            created_at=context_started_at,
            status="started",
        ),
        _event(
            canonical_event="context.build.finished",
            span_id="context-span",
            created_at=context_finished_at,
            status="succeeded",
            latency_ms=0,
            observation_type="span",
            observation_name="context.compile",
        ),
        _event(
            canonical_event="llm.chat.started",
            span_id="llm-span",
            created_at=llm_started_at,
            status="started",
        ),
        _event(
            canonical_event="llm.chat.finished",
            span_id="llm-span",
            created_at=llm_finished_at,
            status="succeeded",
            latency_ms=2000,
            observation_type="generation",
            attributes={"iteration": 1, "wall_latency_ms": 2000},
        ),
    ]

    spans = build_text_otel_span_specs(events)
    context_span = next(span for span in spans if span.name == "context.compile")
    llm_span = next(span for span in spans if span.name == "llm.chat")

    assert context_span.start_time == context_started_at
    assert context_span.end_time == context_finished_at
    assert llm_span.start_time == llm_started_at
    assert llm_span.end_time == llm_finished_at
    assert context_span.end_time <= llm_span.start_time


def _event(
    *,
    canonical_event: str,
    span_id: str,
    created_at: datetime,
    status: str,
    latency_ms: int | None = None,
    observation_type: str | None = None,
    observation_name: str | None = None,
    attributes: dict[str, object] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id="trace-span-timing",
        run_id="run-span-timing",
        node_name="assistant",
        event_type="observability",
        canonical_event=canonical_event,
        observation_type=observation_type,
        observation_name=observation_name,
        observation_scope="iteration",
        span_id=span_id,
        status=status,
        latency_ms=latency_ms,
        attributes={"iteration": 1, **(attributes or {})},
        created_at=created_at,
    )
