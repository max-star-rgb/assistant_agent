"""Session start is the only long-term-memory recall lifecycle."""

from datetime import datetime, timezone
from threading import Lock

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.session_store import InMemorySessionStore


class _CountingMem0Store:
    supports_turn_capture = False

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.recall_count = 0
        self._lock = Lock()

    def recall(
        self,
        identity: RequestIdentity,
        *,
        top_k: int = 5,
    ) -> list[MemoryItem]:
        with self._lock:
            self.recall_count += 1
        if self.fail:
            raise RuntimeError("mem0 unavailable")
        return [
            MemoryItem(
                memory_id=f"memory-{identity.user_id}",
                summary="用户偏好简洁回答",
                created_at=datetime.now(timezone.utc),
            )
        ][:top_k]


def _runtime(store: _CountingMem0Store) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        memory_store=store,
        session_store=InMemorySessionStore(),
    )


def _identity(session_id: str) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="snapshot-user",
        session_id=session_id,
    )


def _request(session_id: str, turn_index: int) -> UserRequest:
    return UserRequest(
        user_id="snapshot-user",
        session_id=session_id,
        text=f"第 {turn_index} 轮",
        metadata={"conversation_turn_index": turn_index},
    )


def test_session_start_recalls_once_and_turns_only_reuse_snapshot() -> None:
    store = _CountingMem0Store()
    runtime = _runtime(store)
    try:
        initialized = runtime.initialize_session_memory(_identity("session-a"))
        first = runtime.run_state(_request("session-a", 1))
        second = runtime.run_state(_request("session-a", 2))

        assert store.recall_count == 1
        assert initialized.request.metadata[
            "memory_session_snapshot_status"
        ] == "loaded"
        assert [item.summary for item in first.memory_context] == [
            "用户偏好简洁回答"
        ]
        assert [item.summary for item in second.memory_context] == [
            "用户偏好简洁回答"
        ]
        assert first.request.metadata[
            "memory_session_snapshot_status"
        ] == "reused"
        assert second.request.metadata[
            "memory_session_snapshot_status"
        ] == "reused"
        first_memory_observations = {
            event.observation_name
            for event in runtime.trace_store.list_by_run(first.run_id)
            if (event.canonical_event or "").startswith("memory.load.")
        }
        assert first_memory_observations == {"memory.session_snapshot"}
    finally:
        runtime.close()


def test_turn_without_session_start_never_recalls() -> None:
    store = _CountingMem0Store()
    runtime = _runtime(store)
    try:
        state = runtime.run_state(_request("not-started", 1))

        assert store.recall_count == 0
        assert state.memory_context == []
        assert state.request.metadata[
            "memory_session_snapshot_status"
        ] == "missing"
    finally:
        runtime.close()


def test_mem0_failure_freezes_empty_snapshot_and_turn_continues() -> None:
    store = _CountingMem0Store(fail=True)
    runtime = _runtime(store)
    try:
        initialized = runtime.initialize_session_memory(_identity("failed"))
        state = runtime.run_state(_request("failed", 1))

        assert store.recall_count == 1
        assert initialized.request.metadata["memory_context_status"] == "degraded"
        assert state.status == "completed"
        assert state.memory_context == []
        assert store.recall_count == 1
    finally:
        runtime.close()


def test_application_session_creation_recalls_before_first_turn() -> None:
    store = _CountingMem0Store()
    runtime = _runtime(store)
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    try:
        session = app.create_session(SessionCreate(user_id="snapshot-user"))
        assert store.recall_count == 1

        runtime.run_state(_request(session.session_id, 1))
        assert store.recall_count == 1
    finally:
        runtime.close()
