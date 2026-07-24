"""Regression coverage for post-response completed-turn memory capture."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.mem0 import Mem0TurnCaptureResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.memory_capture_dispatcher import MemoryCaptureDispatcher
from assistant_agent.services.session_store import InMemorySessionStore


class _BlockingCaptureStore:
    supports_turn_capture = True

    def __init__(self) -> None:
        self.capture_started = Event()
        self.capture_release = Event()

    def recall(self, identity, *, top_k=5):
        return []

    def capture_turn(self, **kwargs) -> Mem0TurnCaptureResult:
        self.capture_started.set()
        if not self.capture_release.wait(2.0):
            raise TimeoutError("test capture was not released")
        return Mem0TurnCaptureResult(
            accepted=True,
            memory_ids=["memory-1"],
        )


def test_runtime_returns_final_state_while_turn_capture_is_still_running() -> None:
    store = _BlockingCaptureStore()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        memory_store=store,
        session_store=InMemorySessionStore(),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            runtime.run_state,
            UserRequest(user_id="capture-user", session_id="capture-session", text="你好"),
        )
        state = future.result(timeout=1.0)

        assert state.status == "completed"
        assert state.request.metadata["memory_capture"]["status"] == "queued"
        assert store.capture_started.wait(0.5) is True
        assert runtime.memory_capture_dispatcher.pending_count == 1

        events = runtime.trace_store.list_by_run(state.run_id)
        canonical_events = [event.canonical_event for event in events]
        assert canonical_events.index("run.completed") < canonical_events.index(
            "memory.capture.queued"
        )
        assert not any(
            event.canonical_event == "memory.capture.finished" for event in events
        )

        store.capture_release.set()
        assert runtime.drain_memory_captures(timeout=1.0) is True
        capture = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.capture.finished"
        )
        assert capture.status == "succeeded"
        assert capture.attributes["memory_count"] == 1
    finally:
        store.capture_release.set()
        runtime.close()
        executor.shutdown(wait=True)


def test_capture_dispatcher_serializes_one_identity_and_parallelizes_other_identities() -> None:
    dispatcher = MemoryCaptureDispatcher(max_workers=2, max_pending=4)
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
        assert dispatcher.submit(ordering_key=("user-a",), callback=first).accepted
        assert first_started.wait(0.5)
        assert dispatcher.submit(ordering_key=("user-a",), callback=second).accepted
        assert dispatcher.submit(ordering_key=("user-b",), callback=other).accepted

        assert other_started.wait(0.5)
        assert not second_started.is_set()
        first_release.set()
        assert dispatcher.drain(timeout=1.0)
        assert order.index("first:end") < order.index("second:start")
    finally:
        first_release.set()
        dispatcher.close(timeout=1.0)


def test_capture_dispatcher_close_drains_accepted_work() -> None:
    dispatcher = MemoryCaptureDispatcher(max_workers=1, max_pending=1)
    capture_started = Event()
    capture_release = Event()
    capture_finished = Event()

    def capture() -> None:
        capture_started.set()
        capture_release.wait(1.0)
        capture_finished.set()

    assert dispatcher.submit(ordering_key=("user",), callback=capture).accepted
    assert capture_started.wait(0.5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        closing = executor.submit(dispatcher.close, timeout=1.0)
        assert not closing.done()
        capture_release.set()
        assert closing.result(timeout=1.0) is True

    assert capture_finished.is_set()
    rejected = dispatcher.submit(ordering_key=("user",), callback=lambda: None)
    assert rejected.accepted is False
    assert rejected.reason == "memory_capture_dispatcher_closed"
