from datetime import datetime, timezone

from fastapi.testclient import TestClient

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.sessions import SessionCreate
from multimodal_agent.schemas.memory_snapshot import MemoryStorageSnapshot
from multimodal_agent.services.assistant_run_service import ConversationTurn, InMemoryConversationStore
from multimodal_agent.services.memory_snapshot import MemorySnapshotService
from multimodal_agent.services.session_store import InMemorySessionStore
from multimodal_agent.services.trace_store import InMemoryTraceStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_memory_snapshot_api_shows_memory_boundaries(monkeypatch) -> None:
    memory_store = InMemoryStore()
    session_store = InMemorySessionStore()
    conversation_store = InMemoryConversationStore()
    runtime = AgentGraphRuntime(
        memory_store=memory_store,
        trace_store=InMemoryTraceStore(),
        session_store=session_store,
    )
    session_store.create(SessionCreate(user_id="u1", title="购物偏好"), session_id="s1")
    conversation_store.append(
        "u1",
        "s1",
        ConversationTurn(
            user_text="我喜欢浅色日系鞋",
            assistant_text="已记录你的偏好。",
            run_id="run_1",
            trace_id="trace_1",
        ),
    )
    memory_store.save(_memory("pref", "preference", "用户喜欢浅色日系风格。", content={"style": "浅色日系"}))
    memory_store.save(_memory("task", "task", "浅色日系商品搜索曾经先比价。"))

    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    client = TestClient(create_app())

    response = client.get(
        "/memory/users/u1/snapshot",
        params={"session_id": "s1", "query": "浅色日系"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "u1"
    assert payload["session"]["thread_id"] == "s1"
    assert payload["conversation_history"]["total"] == 1
    assert payload["conversation_history"]["turns"][0]["user_text"] == "我喜欢浅色日系鞋"
    assert payload["memory_context"]["total"] == 2
    assert [block["layer"] for block in payload["memory_context"]["blocks"]] == ["semantic", "episodic"]
    assert payload["memory_context"]["blocks"][0]["items"][0]["content"] is None
    assert payload["audit"]["total"] == 2
    assert payload["storage"]["memory_store"] == "InMemoryStore"
    assert payload["storage"]["conversation_store"] == "InMemoryConversationStore"
    assert payload["storage"]["langgraph_thread_scope"] == "run_id"


def test_memory_snapshot_api_can_include_sanitized_content(monkeypatch) -> None:
    memory_store = InMemoryStore()
    runtime = AgentGraphRuntime(
        memory_store=memory_store,
        trace_store=InMemoryTraceStore(),
        session_store=InMemorySessionStore(),
    )
    memory_store.save(_memory("pref", "preference", "用户喜欢浅色日系风格。", content={"style": "浅色日系"}))

    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: InMemoryConversationStore())
    client = TestClient(create_app())

    response = client.get(
        "/memory/users/u1/snapshot",
        params={"query": "浅色日系", "include_content": True},
    )

    assert response.status_code == 200
    item = response.json()["memory_context"]["blocks"][0]["items"][0]
    assert item["content"] == {"style": "浅色日系"}


def test_memory_snapshot_service_uses_request_identity_scope() -> None:
    memory_store = InMemoryStore()
    session_store = InMemorySessionStore()
    conversation_store = InMemoryConversationStore()
    runtime = AgentGraphRuntime(
        memory_store=memory_store,
        trace_store=InMemoryTraceStore(),
        session_store=session_store,
    )
    memory_store.save(_memory("pref", "preference", "用户喜欢浅色日系风格。"))
    memory_store.save(
        MemoryItem(
            memory_id="other",
            user_id="u2",
            session_id="s1",
            memory_type="preference",
            summary="另一个用户喜欢浅色日系风格。",
            created_at=NOW,
        )
    )
    service = MemorySnapshotService(
        memory_manager=runtime.memory_manager,
        session_store=session_store,
        conversation_store=conversation_store,
        storage=MemoryStorageSnapshot(
            memory_store="InMemoryStore",
            session_store="InMemorySessionStore",
            conversation_store="InMemoryConversationStore",
            checkpointer="none",
        ),
    )

    snapshot = service.snapshot_for_identity(
        RequestIdentity.for_user(user_id="u1", session_id="s1"),
        query="浅色日系",
    )

    assert snapshot.user_id == "u1"
    assert snapshot.memory_context.total == 1
    assert snapshot.memory_context.blocks[0].items[0].memory_id == "pref"
    assert snapshot.audit.total == 1


def _memory(
    memory_id: str,
    memory_type: str,
    summary: str,
    *,
    content: dict | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        session_id="s1",
        memory_type=memory_type,
        summary=summary,
        content=content or {},
        created_at=NOW,
    )
