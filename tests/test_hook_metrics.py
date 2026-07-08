import json

from assistant_agent.services.hook_metrics import TraceMetricsObserver
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


def _event(
    *,
    canonical_event: str = "run.started",
    status: str | None = "started",
    run_id: str = "run_1",
    trace_id: str = "trace_1",
    node_name: str = "runtime",
    tool_name: str | None = None,
    latency_ms: int | None = None,
    attributes: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id=trace_id,
        run_id=run_id,
        user_id="u1",
        session_id="s1",
        node_name=node_name,
        event_type="observability",
        canonical_event=canonical_event,
        status=status,
        tool_name=tool_name,
        latency_ms=latency_ms,
        attributes=attributes or {},
        error=error,
    )


def test_trace_metrics_observer_collects_redacted_trace_events() -> None:
    observer = TraceMetricsObserver()
    raw_event = _event(attributes={"api_key": "sk-secret-value", "safe_count": 1})

    observer.on_trace_event(raw_event)

    assert len(observer.events) == 1
    dumped = json.dumps([event.model_dump(mode="json") for event in observer.events])
    assert "sk-secret-value" not in dumped
    assert observer.events[0].attributes["safe_count"] == 1


def test_trace_metrics_observer_events_are_defensive_copy() -> None:
    observer = TraceMetricsObserver([_event()])

    observer.events.clear()

    assert len(observer.events) == 1


def test_trace_metrics_observer_summary_uses_existing_metrics_shape() -> None:
    observer = TraceMetricsObserver()
    observer.on_trace_event(_event(canonical_event="run.started", status="started"))
    observer.on_trace_event(
        _event(
            canonical_event="tool.failed",
            status="failed",
            node_name="tool_executor",
            tool_name="product_search",
            latency_ms=80,
            attributes={"retry_count": 1},
            error={"code": "provider_timeout", "message": "Provider timed out"},
        )
    )
    observer.on_trace_event(_event(canonical_event="run.failed", status="failed"))

    metrics = observer.summary()

    assert metrics["event_count"] == 3
    assert metrics["run"]["count"] == 1
    assert metrics["run"]["failed"] == 1
    assert metrics["tools"]["total_calls"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["failure_count"] == 1


def test_trace_metrics_observer_clear_resets_local_state() -> None:
    observer = TraceMetricsObserver([_event()])

    observer.clear()

    assert observer.events == []
    assert observer.summary()["event_count"] == 0


def test_trace_metrics_observer_composes_through_hook_trace_store_without_changing_primary_reads() -> None:
    primary = InMemoryTraceStore()
    observer = TraceMetricsObserver()
    manager = HookManager([observer])
    store = CompositeTraceStore(primary, [HookTraceStore(manager)])
    event = _event()

    store.append(event)

    assert store.list_by_run("run_1") == primary.list_by_run("run_1")
    assert observer.summary()["event_count"] == 1
    assert manager.errors == []
