"""Regression coverage for post-response completed-turn memory ingestion."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.memory.session_snapshot import SessionMemorySnapshotStore
from assistant_agent.memory.mem0.models import Mem0IngestionResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.session_store import InMemorySessionStore


class _BlockingIngestionClient:
    def __init__(self) -> None:
        self.ingestion_started = Event()
        self.ingestion_release = Event()

    configured = True

    def recall_long_term_memory(self, identity, *, top_k=5):
        return []

    def ingest_completed_turn(self, turn) -> Mem0IngestionResult:
        self.ingestion_started.set()
        if not self.ingestion_release.wait(2.0):
            raise TimeoutError("test ingestion was not released")
        return Mem0IngestionResult(
            accepted=True,
            memory_ids=["memory-1"],
        )


def test_runtime_returns_final_state_while_turn_ingestion_is_still_running() -> None:
    client = _BlockingIngestionClient()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        long_term_memory_service=LongTermMemoryService(
            client=client,
            snapshot_store=SessionMemorySnapshotStore(),
            ingestion_queue=MemoryIngestionQueue(),
        ),
        session_store=InMemorySessionStore(),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            runtime.run_state,
            UserRequest(user_id="ingestion-user", session_id="ingestion-session", text="你好"),
        )
        state = future.result(timeout=1.0)

        assert state.status == "completed"
        assert state.request.metadata["memory_ingestion"]["status"] == "queued"
        assert client.ingestion_started.wait(0.5) is True
        assert runtime.long_term_memory_service.ingestion_queue.pending_count == 1

        events = runtime.trace_store.list_by_run(state.run_id)
        canonical_events = [event.canonical_event for event in events]
        assert canonical_events.index("run.completed") < canonical_events.index(
            "memory.ingestion.queued"
        )
        assert not any(
            event.canonical_event == "memory.ingestion.finished" for event in events
        )

        client.ingestion_release.set()
        assert runtime.drain_memory_ingestions(timeout=1.0) is True
        ingestion = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.ingestion.finished"
        )
        assert ingestion.status == "succeeded"
        assert ingestion.attributes["memory_count"] == 1
    finally:
        client.ingestion_release.set()
        runtime.close()
        executor.shutdown(wait=True)


def test_ingestion_queue_serializes_one_identity_and_parallelizes_other_identities() -> None:
    queue = MemoryIngestionQueue(max_workers=2, max_pending=4)
    first_started = Event()
    first_release = Event()
    second_started = Event()
    other_started = Event()
    order: list[str] = []
    order_lock = Lock()

    def record(value: str) -> None:
        with order_lock:
            order.append(value)

    def first() -> None:
        record("first:start")
        first_started.set()
        first_release.wait(1.0)
        record("first:end")

    def second() -> None:
        record("second:start")
        second_started.set()

    def other() -> None:
        record("other:start")
        other_started.set()

    try:
        assert queue.submit(ordering_key=("user-a",), callback=first).accepted
        assert first_started.wait(0.5)
        assert queue.submit(ordering_key=("user-a",), callback=second).accepted
        assert queue.submit(ordering_key=("user-b",), callback=other).accepted

        assert other_started.wait(0.5)
        assert not second_started.is_set()
        first_release.set()
        assert queue.drain(timeout=1.0)
        assert order.index("first:end") < order.index("second:start")
    finally:
        first_release.set()
        queue.close(timeout=1.0)


def test_ingestion_queue_close_drains_accepted_work() -> None:
    queue = MemoryIngestionQueue(max_workers=1, max_pending=1)
    ingestion_started = Event()
    ingestion_release = Event()
    ingestion_finished = Event()

    def ingest() -> None:
        ingestion_started.set()
        ingestion_release.wait(1.0)
        ingestion_finished.set()

    assert queue.submit(ordering_key=("user",), callback=ingest).accepted
    assert ingestion_started.wait(0.5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        closing = executor.submit(queue.close, timeout=1.0)
        assert not closing.done()
        ingestion_release.set()
        assert closing.result(timeout=1.0) is True

    assert ingestion_finished.is_set()
    rejected = queue.submit(ordering_key=("user",), callback=lambda: None)
    assert rejected.accepted is False
    assert rejected.reason == "memory_ingestion_queue_closed"
