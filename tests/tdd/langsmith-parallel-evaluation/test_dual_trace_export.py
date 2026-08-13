from __future__ import annotations

from datetime import datetime, timezone
import time

import pytest

from assistant_agent.observability import otel_exporter
from assistant_agent.observability import trace_persistence as persistence
from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.runtime.hooks import HookManager, HookTraceStore


def _observer_labels(store: object) -> list[object]:
    labels: list[object] = []
    for secondary in store.secondaries:
        if isinstance(secondary, HookTraceStore):
            labels.extend(secondary.manager.observers)
    return labels


def _terminal_event() -> TraceEvent:
    return TraceEvent(
        trace_id="a" * 32,
        run_id="run-1",
        node_name="runtime",
        event_type="observability",
        canonical_event="assistant.turn.summary",
        status="completed",
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def test_server_store_keeps_only_local_ledger_and_generic_otel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        lambda: "generic-otel",
    )
    monkeypatch.setattr(
        persistence,
        "create_langsmith_text_otel_trace_observer_from_env",
        lambda: pytest.fail("native tracing owns LangSmith"),
        raising=False,
    )
    store = persistence.create_server_trace_store(path=tmp_path / "trace.jsonl")
    try:
        store.append(_terminal_event())
        assert _observer_labels(store) == ["generic-otel"]
        assert store.list_by_run("run-1") == [_terminal_event()]
    finally:
        persistence.close_trace_store(store)


def test_one_observer_failure_does_not_block_the_other() -> None:
    calls: list[TraceEvent] = []

    class FailingObserver:
        def on_trace_event(self, event: TraceEvent) -> None:
            raise RuntimeError("observer failed")

    class RecordingObserver:
        def on_trace_event(self, event: TraceEvent) -> None:
            calls.append(event)

    manager = HookManager([FailingObserver(), RecordingObserver()])
    manager.on_trace_event(_terminal_event())

    assert len(calls) == 1
    assert len(manager.errors) == 1


def test_observer_close_uses_one_parallel_deadline() -> None:
    calls: list[float] = []

    class Observer:
        def close(self, *, timeout: float) -> bool:
            calls.append(timeout)
            time.sleep(timeout)
            return True

    store = HookTraceStore(HookManager([Observer(), Observer()]))
    started = time.monotonic()

    assert store.close(timeout=0.05) is False

    elapsed = time.monotonic() - started
    assert len(calls) == 2
    assert elapsed < 0.08


def test_observer_close_exception_does_not_skip_other_observer() -> None:
    calls: list[str] = []

    class FailingObserver:
        def close(self, *, timeout: float) -> bool:
            calls.append("failing")
            raise RuntimeError("close failed")

    class RecordingObserver:
        def close(self, *, timeout: float) -> bool:
            calls.append("recording")
            return True

    manager = HookManager([RecordingObserver(), FailingObserver()])

    assert HookTraceStore(manager).close(timeout=0.05) is False
    assert sorted(calls) == ["failing", "recording"]
    assert len(manager.errors) == 1


@pytest.mark.parametrize("failure_mode", ["raise", "false"])
def test_buffered_exporter_reports_sink_lifecycle_failure(failure_mode: str) -> None:
    class Sink:
        def export(self, spans) -> bool:
            return True

        def flush(self) -> bool:
            if failure_mode == "raise":
                raise RuntimeError("flush failed")
            return False

        def shutdown(self) -> bool:
            return False

    exporter = otel_exporter.BufferedTextOtelSpanExporter(Sink())

    assert exporter.flush(timeout=0.2) is False
    assert exporter.close(timeout=0.2) is False
    assert exporter.error_count >= 2
    assert exporter.worker_alive is False
