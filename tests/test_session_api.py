from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.services.assistant_run_service import ConversationTurn, get_default_conversation_store
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import InMemoryTraceStore


def test_session_api_can_create_list_get_and_delete_sessions(monkeypatch) -> None:
    runtime = AgentGraphRuntime(
        memory_store=InMemoryStore(),
        trace_store=InMemoryTraceStore(),
        session_store=InMemorySessionStore(),
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    created = client.post("/sessions", json={"user_id": "u1", "title": "购物"}).json()
    listed = client.get("/sessions", params={"user_id": "u1"}).json()
    fetched = client.get(f"/sessions/{created['session_id']}", params={"user_id": "u1"}).json()
    deleted = client.delete(f"/sessions/{created['session_id']}", params={"user_id": "u1"})

    assert created["thread_id"] == created["session_id"]
    assert listed["total"] == 1
    assert listed["sessions"][0]["session_id"] == created["session_id"]
    assert fetched["title"] == "购物"
    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["sessions"] == 1
    assert client.get(f"/sessions/{created['session_id']}", params={"user_id": "u1"}).status_code == 404


def test_agent_run_updates_session_index(monkeypatch) -> None:
    runtime = AgentGraphRuntime(
        memory_store=InMemoryStore(),
        trace_store=InMemoryTraceStore(),
        session_store=InMemorySessionStore(),
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    run = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "shopping_thread", "text": "帮我找相似款"},
    ).json()
    session = client.get("/sessions/shopping_thread", params={"user_id": "u1"}).json()

    assert session["thread_id"] == "shopping_thread"
    assert session["run_count"] == 1
    assert session["last_run_id"] == run["run_id"]
    assert session["last_trace_id"] == run["trace_id"]
    assert session["last_message_preview"] == "帮我找相似款"


def test_delete_session_api_clears_jsonl_conversation_history(monkeypatch, tmp_path) -> None:
    config = ProviderConfig(
        conversation_history_backend="jsonl",
        conversation_history_path=str(tmp_path / "conversation_history.jsonl"),
    )
    runtime = AgentGraphRuntime(
        config=config,
        memory_store=InMemoryStore(),
        trace_store=InMemoryTraceStore(),
    )
    runtime.session_store.touch_run(
        user_id="u1",
        session_id="s1",
        run_id="run_1",
        trace_id="trace_1",
        message_preview="第一轮",
        status="completed",
    )
    conversation_store = get_default_conversation_store(config)
    conversation_store.append(
        "u1",
        "s1",
        ConversationTurn(
            user_text="第一轮",
            assistant_text="已记录。",
            run_id="run_1",
            trace_id="trace_1",
        ),
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    deleted = client.delete("/sessions/s1", params={"user_id": "u1"})

    assert deleted.status_code == 200
    assert runtime.session_store.get("u1", "s1") is None
    assert conversation_store.get("u1", "s1") == []
