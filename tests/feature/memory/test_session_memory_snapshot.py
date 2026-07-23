"""Stable session-scoped long-term memory snapshot behavior."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.manager import MemoryContext
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.session_memory_context import SessionMemoryContextStore
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_metrics import build_trace_metrics


class _CountingCoreMemoryStore(InMemoryStore):
    always_load_core_memory = True

    def __init__(self) -> None:
        super().__init__()
        self.search_count = 0
        self.queries: list[str] = []
        self._count_lock = Lock()

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        with self._count_lock:
            self.search_count += 1
            self.queries.append(query.query)
        now = datetime.now(timezone.utc)
        item = MemoryItem(
            memory_id=f"core-{query.user_id}",
            user_id=query.user_id,
            tenant_id=query.tenant_id,
            project_id=query.project_id,
            scope="project",
            memory_type="preference",
            content={"record_kind": "core"},
            summary="用户偏好简洁回答",
            source="test_core_memory",
            created_at=now,
            updated_at=now,
        )
        return MemorySearchResult(
            items=[item],
            query_used=query,
            total=1,
            ranking_reason="test",
            memory_context=item.summary,
        )


class _FailingCoreMemoryStore(_CountingCoreMemoryStore):
    def search(self, query: MemoryQuery) -> MemorySearchResult:
        with self._count_lock:
            self.search_count += 1
            self.queries.append(query.query)
        raise RuntimeError("unavailable")


def _runtime(
    store: _CountingCoreMemoryStore,
    *,
    snapshot_max_entries: int = 1024,
) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            memory_session_snapshot_max_entries=snapshot_max_entries,
        ),
        memory_store=store,
        session_store=InMemorySessionStore(),
    )


def _request(*, session_id: str, turn_index: int, reset: bool = False) -> UserRequest:
    return UserRequest(
        user_id="snapshot-user",
        session_id=session_id,
        text=f"第 {turn_index} 轮",
        metadata={
            "conversation_turn_index": turn_index,
            **({"reset_conversation": True} if reset else {}),
        },
    )


def _identity(session_id: str) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="snapshot-user",
        session_id=session_id,
    )


def test_session_start_recalls_once_before_turns_reuse_memory_snapshot() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store)
    try:
        initialized = runtime.initialize_session_memory(_identity("session-a"))
        first = runtime.run_state(_request(session_id="session-a", turn_index=1))
        second = runtime.run_state(_request(session_id="session-a", turn_index=2))

        assert store.search_count == 1
        assert store.queries == [""]
        assert initialized.request.metadata["memory_session_snapshot_status"] == "loaded"
        assert initialized.request.metadata["memory_context_source"] == "long_term_recall"
        assert [item.memory_id for item in first.memory_context] == [
            "core-snapshot-user"
        ]
        assert [item.memory_id for item in second.memory_context] == [
            "core-snapshot-user"
        ]
        assert first.request.metadata["memory_session_snapshot_status"] == "reused"
        assert first.request.metadata["memory_context_source"] == "session_snapshot"
        assert second.request.metadata["memory_session_snapshot_status"] == "reused"
        assert second.request.metadata["memory_context_source"] == "session_snapshot"

        initialization_load = next(
            event
            for event in runtime.trace_store.list_by_run(initialized.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert initialization_load.node_name == "session_start"
        assert initialization_load.observation_name == "memory.core_recall"
        assert initialization_load.attributes["retrieval_count"] == 1

        first_load = next(
            event
            for event in runtime.trace_store.list_by_run(first.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert first_load.observation_name == "memory.session_snapshot"
        assert first_load.attributes["retrieval_count"] == 0

        second_load = next(
            event
            for event in runtime.trace_store.list_by_run(second.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert second_load.observation_name == "memory.session_snapshot"
        assert second_load.attributes["retrieval_count"] == 0
        assert build_trace_metrics(
            runtime.trace_store.list_by_run(second.run_id)
        )["memory"]["retrieval_count"] == 0
    finally:
        runtime.close()


def test_first_turn_without_session_start_never_triggers_recall() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store)
    try:
        state = runtime.run_state(_request(session_id="session-not-started", turn_index=1))

        assert store.search_count == 0
        assert state.memory_context == []
        assert state.request.metadata["memory_session_snapshot_status"] == "missing"
        load = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert load.observation_name == "memory.session_snapshot"
        assert load.attributes["retrieval_count"] == 0
    finally:
        runtime.close()


def test_session_start_recall_failure_freezes_empty_snapshot_and_turn_continues() -> None:
    store = _FailingCoreMemoryStore()
    runtime = _runtime(store)
    try:
        initialized = runtime.initialize_session_memory(_identity("session-failed"))
        state = runtime.run_state(
            _request(session_id="session-failed", turn_index=1)
        )

        assert store.search_count == 1
        assert initialized.request.metadata["memory_context_source"] == (
            "session_start_fallback"
        )
        assert initialized.request.metadata["memory_context_policy_reason"] == (
            "session_memory_initialization_failed"
        )
        assert state.status == "completed"
        assert state.request.metadata["memory_session_snapshot_status"] == "reused"
        assert state.request.metadata["memory_context_policy_reason"] == (
            "session_memory_initialization_failed"
        )
        assert store.search_count == 1

        initialization_load = next(
            event
            for event in runtime.trace_store.list_by_run(initialized.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert initialization_load.status == "failed"
        turn_load = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert turn_load.observation_name == "memory.session_snapshot"
        assert turn_load.status == "degraded"
    finally:
        runtime.close()


def test_application_session_creation_recalls_before_first_turn() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store)
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    try:
        session = app.create_session(SessionCreate(user_id="snapshot-user"))

        assert store.search_count == 1
        initialization_load = next(
            event
            for event in runtime.trace_store.list_by_user("snapshot-user")
            if event.canonical_event == "memory.load.finished"
        )
        assert initialization_load.session_id == session.session_id
        assert initialization_load.node_name == "session_start"
        assert initialization_load.observation_name == "memory.core_recall"

        first = runtime.run_state(
            _request(session_id=session.session_id, turn_index=1)
        )

        assert store.search_count == 1
        first_load = next(
            event
            for event in runtime.trace_store.list_by_run(first.run_id)
            if event.canonical_event == "memory.load.finished"
        )
        assert first_load.observation_name == "memory.session_snapshot"
        assert first_load.attributes["retrieval_count"] == 0
    finally:
        runtime.close()


def test_new_session_start_or_explicit_session_reset_creates_new_snapshot() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store)
    try:
        runtime.initialize_session_memory(_identity("session-a"))
        runtime.initialize_session_memory(_identity("session-b"))
        reset = runtime.initialize_session_memory(
            _identity("session-a"),
            reset=True,
        )

        assert store.search_count == 3
        assert reset.request.metadata["memory_session_snapshot_status"] == "loaded"
    finally:
        runtime.close()


def test_later_turn_without_retained_snapshot_does_not_recall_again() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store)
    try:
        state = runtime.run_state(_request(session_id="session-late", turn_index=2))

        assert store.search_count == 0
        assert state.memory_context == []
        assert state.request.metadata["memory_session_snapshot_status"] == "missing"
        assert state.request.metadata["memory_context_policy_reason"] == (
            "session_memory_snapshot_missing"
        )
    finally:
        runtime.close()


def test_evicted_later_turn_does_not_repeat_long_term_memory_recall() -> None:
    store = _CountingCoreMemoryStore()
    runtime = _runtime(store, snapshot_max_entries=1)
    try:
        runtime.initialize_session_memory(_identity("session-a"))
        runtime.initialize_session_memory(_identity("session-b"))
        evicted = runtime.run_state(_request(session_id="session-a", turn_index=2))

        assert store.search_count == 2
        assert evicted.memory_context == []
        assert evicted.request.metadata["memory_session_snapshot_status"] == "missing"
    finally:
        runtime.close()


def test_concurrent_first_and_later_turn_share_one_snapshot_load() -> None:
    snapshots = SessionMemoryContextStore(max_entries=4)
    identity = RequestIdentity.for_user(
        user_id="snapshot-user",
        session_id="session-concurrent",
    )
    load_started = Event()
    load_release = Event()
    load_count = 0
    count_lock = Lock()

    def loader() -> MemoryContext:
        nonlocal load_count
        with count_lock:
            load_count += 1
        load_started.set()
        load_release.wait(1.0)
        return MemoryContext(text="snapshot")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            snapshots.resolve,
            identity,
            loader=loader,
            allow_load=True,
        )
        assert load_started.wait(0.5)
        later = executor.submit(
            snapshots.resolve,
            identity,
            loader=loader,
            allow_load=False,
        )
        load_release.set()
        first_result = first.result(timeout=1.0)
        later_result = later.result(timeout=1.0)

    assert load_count == 1
    assert first_result.status == "loaded"
    assert later_result.status == "reused"
    assert later_result.context is not None
    assert later_result.context.text == "snapshot"
