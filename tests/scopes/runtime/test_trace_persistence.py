from __future__ import annotations

import threading
import time

from assistant_agent.services.trace_persistence import (
    BufferedJsonlTraceStore,
    close_trace_store,
    create_server_trace_store,
)
from assistant_agent.services.trace_store import (
    CompositeTraceStore,
    InMemoryTraceStore,
    JsonlTraceStore,
    TraceEvent,
)


def _event(index: int = 1, *, user_id: str = "u1") -> TraceEvent:
    return TraceEvent(
        trace_id=f"trace_{index}",
        run_id=f"run_{index}",
        user_id=user_id,
        session_id="s1",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.started",
    )


def test_buffered_jsonl_store_flushes_without_delaying_primary_reads(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    primary = InMemoryTraceStore()
    secondary = BufferedJsonlTraceStore(JsonlTraceStore(path), capacity=4)
    store = CompositeTraceStore(primary, [secondary])

    store.append(_event())

    assert primary.list_by_run("run_1")
    assert secondary.flush(timeout=1.0) is True
    assert JsonlTraceStore(path).list_by_run("run_1")
    assert close_trace_store(store, timeout=1.0) is True
    assert secondary.worker_alive is False


def test_buffered_jsonl_store_drops_when_queue_is_full_without_blocking(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingSink(InMemoryTraceStore):
        def append(self, event: TraceEvent) -> None:
            started.set()
            assert release.wait(timeout=2)
            super().append(event)

    secondary = BufferedJsonlTraceStore(BlockingSink(), capacity=1)
    secondary.append(_event(1))
    assert started.wait(timeout=1)
    secondary.append(_event(2))

    before = time.monotonic()
    secondary.append(_event(3))
    elapsed = time.monotonic() - before

    assert elapsed < 0.1
    assert secondary.dropped_event_count == 1
    release.set()
    assert secondary.close(timeout=1.0) is True


def test_buffered_jsonl_store_serializes_delete_by_user(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    secondary = BufferedJsonlTraceStore(JsonlTraceStore(path))
    secondary.append(_event(1, user_id="u1"))
    secondary.append(_event(2, user_id="u2"))
    assert secondary.flush(timeout=1.0) is True

    assert secondary.delete_by_user("u1") == 1
    assert secondary.flush(timeout=1.0) is True
    assert JsonlTraceStore(path).list_by_user("u1") == []
    assert len(JsonlTraceStore(path).list_by_user("u2")) == 1
    assert secondary.close(timeout=1.0) is True


def test_server_trace_store_persists_redacted_events(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    store = create_server_trace_store(path=path)
    store.append(
        TraceEvent(
            trace_id="trace_safe",
            run_id="run_safe",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.failed",
            attributes={
                "api_key": "sk-secret-value",
                "frame_path": "/home/user/private/frame.jpg",
                "provider_raw_response": "private response",
            },
        )
    )

    assert close_trace_store(store, timeout=1.0) is True
    raw = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    assert "/home/user/private/frame.jpg" not in raw
    assert "private response" not in raw


def test_composite_trace_store_closes_secondaries_before_primary() -> None:
    calls = []

    class ClosableStore(InMemoryTraceStore):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        def close(self, *, timeout: float) -> bool:
            assert timeout >= 0
            calls.append(self.name)
            return True

    primary = ClosableStore("primary")
    first = ClosableStore("first")
    second = ClosableStore("second")
    store = CompositeTraceStore(primary, [first, second])

    assert store.close(timeout=1.0) is True
    assert calls == ["second", "first", "primary"]
