from __future__ import annotations

import importlib.util

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace

from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs


def test_experiment_runtime_host_propagates_current_parent_and_closes_owned_resources() -> None:
    assert importlib.util.find_spec("assistant_agent.evaluation.experiment_runtime") is not None

    from assistant_agent.evaluation.experiment_runtime import (
        create_experiment_runtime_host,
    )

    lifecycle: list[str] = []

    class TraceStore:
        def close(self, *, timeout: float) -> bool:
            assert timeout > 0
            lifecycle.append("trace_store")
            return True

    class Runtime:
        def __init__(self, trace_store) -> None:
            self.trace_store = trace_store
            self.contexts = []

        def run_state(self, request, *, trace_context=None):
            self.contexts.append(trace_context)
            return request

        def close(self) -> bool:
            lifecycle.append("runtime")
            return True

    trace_store = TraceStore()
    runtime_holder = []

    def build_runtime(store):
        runtime = Runtime(store)
        runtime_holder.append(runtime)
        return runtime

    host = create_experiment_runtime_host(
        build_runtime,
        trace_store_factory=lambda: trace_store,
    )
    span_context = otel_trace.SpanContext(
        trace_id=int("1" * 32, 16),
        span_id=int("2" * 16, 16),
        is_remote=False,
        trace_flags=otel_trace.TraceFlags(1),
        trace_state=otel_trace.TraceState(),
    )
    token = otel_context.attach(
        otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(span_context))
    )
    try:
        assert host.run_state("request-sentinel") == "request-sentinel"
    finally:
        otel_context.detach(token)

    propagated = runtime_holder[0].contexts[0]
    assert propagated.trace_id == "1" * 32
    assert propagated.parent_span_id == "2" * 16
    assert host.close(timeout=2.0) is True
    assert host.close(timeout=2.0) is True
    assert lifecycle == ["runtime", "trace_store"]


def test_experiment_runtime_host_rejects_missing_active_parent_before_runtime_call() -> None:
    assert importlib.util.find_spec("assistant_agent.evaluation.experiment_runtime") is not None

    from assistant_agent.evaluation.experiment_runtime import (
        create_experiment_runtime_host,
    )

    class TraceStore:
        def close(self, *, timeout: float) -> bool:
            return True

    class Runtime:
        trace_store = None

        def run_state(self, request, *, trace_context=None):
            raise AssertionError("runtime must not run without the Experiment parent span")

        def close(self) -> bool:
            return True

    host = create_experiment_runtime_host(
        lambda store: Runtime(),
        trace_store_factory=TraceStore,
    )
    try:
        try:
            host.run_state("request-sentinel")
        except RuntimeError as exc:
            assert str(exc) == "Langfuse Experiment task has no active OTel parent span"
        else:
            raise AssertionError("missing Experiment parent must fail closed")
    finally:
        host.close(timeout=2.0)


def test_experiment_trace_store_requires_otel_export_and_excludes_live_score_writer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import assistant_agent.observability.trace_persistence as persistence

    assert hasattr(persistence, "create_experiment_trace_store")
    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="OTel trace export"):
        persistence.create_experiment_trace_store(path=tmp_path / "missing.jsonl")

    observed = []

    class Observer:
        def on_trace_event(self, event) -> None:
            observed.append(event)

        def close(self, *, timeout: float) -> bool:
            return True

    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        Observer,
    )
    monkeypatch.setattr(
        persistence,
        "create_langfuse_score_trace_observer_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("Experiment must not emit legacy/live runtime scores")
        ),
    )
    store = persistence.create_experiment_trace_store(path=tmp_path / "ready.jsonl")
    event = TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.completed",
    )
    store.append(event)
    assert observed == [event]
    assert persistence.close_trace_store(store, timeout=2.0) is True


def test_runtime_nested_under_external_trace_does_not_overwrite_trace_name() -> None:
    events = [
        TraceEvent(
            trace_id="1" * 32,
            run_id="run-sentinel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            observation_type="span",
            observation_name="agent.runtime",
            span_id="3" * 16,
            parent_span_id="2" * 16,
        ),
        TraceEvent(
            trace_id="1" * 32,
            run_id="run-sentinel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            observation_type="span",
            observation_name="agent.runtime",
            span_id="3" * 16,
            parent_span_id="2" * 16,
            status="completed",
        ),
    ]

    root = build_text_otel_span_specs(events)[0]

    assert root.parent_span_id == "2" * 16
    assert "langfuse.trace.name" not in root.attributes
