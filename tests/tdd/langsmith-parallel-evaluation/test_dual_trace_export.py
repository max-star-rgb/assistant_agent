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


def test_server_store_registers_langfuse_and_langsmith_observers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        lambda: "langfuse",
    )
    monkeypatch.setattr(
        persistence,
        "create_langsmith_text_otel_trace_observer_from_env",
        lambda: "langsmith",
        raising=False,
    )
    monkeypatch.setattr(
        persistence,
        "create_langfuse_score_trace_observer_from_env",
        lambda: None,
    )

    store = persistence.create_server_trace_store(path=tmp_path / "trace.jsonl")
    try:
        assert _observer_labels(store) == ["langfuse", "langsmith"]
    finally:
        persistence.close_trace_store(store)


def test_langsmith_observer_factory_applies_experiment_project_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    monkeypatch.setattr(
        otel_exporter,
        "create_text_otel_trace_observer",
        lambda config: captured.append(config) or "langsmith",
        raising=False,
    )

    observer = otel_exporter.create_langsmith_text_otel_trace_observer_from_env(
        {
            "ASSISTANT_AGENT_LANGSMITH_ENABLED": "true",
            "LANGSMITH_API_KEY": "test-key",
            "LANGSMITH_PROJECT": "daily-project",
        },
        project_override="experiment-project",
        required=True,
    )

    assert observer == "langsmith"
    assert captured[0].headers["Langsmith-Project"] == "experiment-project"


def test_langsmith_experiment_store_excludes_langfuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    langfuse_calls: list[bool] = []
    langsmith_projects: list[str] = []
    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        lambda: langfuse_calls.append(True) or "langfuse",
    )
    monkeypatch.setattr(
        persistence,
        "create_langsmith_text_otel_trace_observer_from_env",
        lambda *, project_override, required: (
            langsmith_projects.append(project_override) or "langsmith"
        ),
        raising=False,
    )

    store = persistence.create_langsmith_experiment_trace_store(
        project_id="experiment-id",
        path=tmp_path / "trace.jsonl",
    )
    try:
        assert _observer_labels(store) == ["langsmith"]
        assert langsmith_projects == ["experiment-id"]
        assert langfuse_calls == []
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


def test_langsmith_close_cannot_consume_langfuse_close_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    close_calls: list[tuple[str, float]] = []

    class Observer:
        def __init__(self, name: str, *, delay: float = 0.0) -> None:
            self.name = name
            self.delay = delay

        def close(self, *, timeout: float) -> bool:
            close_calls.append((self.name, timeout))
            if self.delay:
                time.sleep(self.delay)
            return self.name != "langsmith"

    monkeypatch.setattr(
        persistence,
        "create_text_otel_trace_observer_from_env",
        lambda: Observer("langfuse"),
    )
    monkeypatch.setattr(
        persistence,
        "create_langsmith_text_otel_trace_observer_from_env",
        lambda: Observer("langsmith", delay=0.02),
    )
    monkeypatch.setattr(
        persistence,
        "create_langfuse_score_trace_observer_from_env",
        lambda: None,
    )
    store = persistence.create_server_trace_store(path=tmp_path / "trace.jsonl")

    assert persistence.close_trace_store(store, timeout=0.01) is False
    assert [name for name, _ in close_calls] == ["langsmith", "langfuse"]
    assert close_calls[1][1] > 0


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
