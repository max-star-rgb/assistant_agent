from __future__ import annotations

from types import SimpleNamespace

from assistant_agent.memory.mem0.models import Mem0MemoryChange
from assistant_agent.memory.observability import record_ingestion_finished
from assistant_agent.memory.trace_content import (
    InMemoryMemoryTraceContentStore,
    MemoryIngestionTraceContent,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore


def _state(*, trace_id: str = "trace-sentinel", run_id: str = "run-sentinel"):
    return SimpleNamespace(
        trace_id=trace_id,
        run_id=run_id,
        user_id="user-sentinel",
        session_id="session-sentinel",
    )


def _changes() -> list[Mem0MemoryChange]:
    return [
        Mem0MemoryChange(
            memory_id="memory-add-sentinel",
            memory="用户偏好使用中文",
            event="ADD",
        ),
        Mem0MemoryChange(
            memory_id="memory-delete-sentinel",
            memory=None,
            event="DELETE",
        ),
    ]


def test_canonical_ingestion_event_excludes_memory_text_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT", raising=False)
    trace_store = InMemoryTraceStore()
    content_store = InMemoryMemoryTraceContentStore()

    record_ingestion_finished(
        trace_store=trace_store,
        state=_state(),
        status="succeeded",
        latency_ms=17,
        changes=_changes(),
        source_turn="source-turn-sentinel",
        content_store=content_store,
    )

    event = trace_store.events[-1]
    assert event.attributes == {
        "memory_count": 2,
        "change_counts": {"ADD": 1, "DELETE": 1},
        "memory_ids": ["memory-add-sentinel", "memory-delete-sentinel"],
        "source_turn": "source-turn-sentinel",
        "errors": [],
        "content_capture_status": "disabled",
    }
    assert "用户偏好使用中文" not in repr(event.model_dump(mode="json"))
    assert content_store.get(trace_id="trace-sentinel", run_id="run-sentinel") is None


def test_legacy_memory_ids_remain_in_canonical_summary() -> None:
    trace_store = InMemoryTraceStore()

    record_ingestion_finished(
        trace_store=trace_store,
        state=_state(),
        status="succeeded",
        latency_ms=17,
        memory_count=1,
        memory_ids=["memory-legacy-sentinel"],
        changes=None,
        source_turn="source-turn-sentinel",
    )

    assert trace_store.events[-1].attributes["memory_count"] == 1
    assert trace_store.events[-1].attributes["memory_ids"] == [
        "memory-legacy-sentinel"
    ]


def test_explicit_local_policy_captures_memory_changes_only_in_overlay(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT", "1")
    trace_store = InMemoryTraceStore()
    content_store = InMemoryMemoryTraceContentStore()

    record_ingestion_finished(
        trace_store=trace_store,
        state=_state(),
        status="succeeded",
        latency_ms=17,
        changes=_changes(),
        source_turn="source-turn-sentinel",
        content_store=content_store,
    )

    content = content_store.get(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
    )
    assert content is not None
    assert content.source_turn == "source-turn-sentinel"
    assert [change.memory for change in content.changes] == [
        "用户偏好使用中文",
        None,
    ]
    assert "用户偏好使用中文" not in repr(trace_store.events[-1].model_dump(mode="json"))


def test_memory_content_permission_does_not_enable_other_trace_content(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "0")
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT", "1")
    trace_store = InMemoryTraceStore()
    content_store = InMemoryMemoryTraceContentStore()

    record_ingestion_finished(
        trace_store=trace_store,
        state=_state(),
        status="succeeded",
        latency_ms=17,
        changes=_changes(),
        source_turn="source-turn-sentinel",
        content_store=content_store,
    )

    assert content_store.get(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
    ) is not None


def test_overlay_failure_does_not_block_canonical_event(monkeypatch) -> None:
    class ThrowingStore:
        def put(self, content) -> None:
            raise RuntimeError("overlay-write-sentinel")

    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT", "1")
    trace_store = InMemoryTraceStore()

    record_ingestion_finished(
        trace_store=trace_store,
        state=_state(),
        status="succeeded",
        latency_ms=17,
        changes=_changes(),
        source_turn="source-turn-sentinel",
        content_store=ThrowingStore(),
    )

    assert trace_store.events[-1].canonical_event == "memory.ingestion.finished"
    assert trace_store.events[-1].attributes["memory_count"] == 2
    assert trace_store.events[-1].attributes["content_capture_status"] == "failed"


def test_memory_trace_content_store_evicts_oldest_record() -> None:
    store = InMemoryMemoryTraceContentStore(max_entries=1)
    store.put(
        MemoryIngestionTraceContent(
            trace_id="trace-old-sentinel",
            run_id="run-old-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            source_turn="source-turn-old-sentinel",
            changes=_changes(),
        )
    )
    store.put(
        MemoryIngestionTraceContent(
            trace_id="trace-new-sentinel",
            run_id="run-new-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            source_turn="source-turn-new-sentinel",
            changes=[],
        )
    )

    assert store.get(
        trace_id="trace-old-sentinel",
        run_id="run-old-sentinel",
    ) is None
    assert store.get(
        trace_id="trace-new-sentinel",
        run_id="run-new-sentinel",
    ) is not None
