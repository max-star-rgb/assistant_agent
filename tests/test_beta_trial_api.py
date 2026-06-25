from datetime import datetime, timezone

from fastapi.testclient import TestClient

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.services.beta_feedback import InMemoryBetaFeedbackStore
from multimodal_agent.services.trace_store import InMemoryTraceStore


def test_beta_feedback_is_bound_to_run_user(monkeypatch) -> None:
    runtime = AgentGraphRuntime(memory_store=InMemoryStore(), trace_store=InMemoryTraceStore())
    feedback_store = InMemoryBetaFeedbackStore()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_beta_feedback_store", lambda: feedback_store)
    client = TestClient(create_app())

    run = client.post("/agent/run", json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"}).json()

    accepted = client.post(
        "/beta/feedback",
        json={
            "run_id": run["run_id"],
            "user_id": "u1",
            "rating": "down",
            "category": "bad_tool_choice",
            "note": "选错工具了",
        },
    )
    rejected = client.post(
        "/beta/feedback",
        json={
            "run_id": run["run_id"],
            "user_id": "u2",
            "rating": "down",
            "category": "privacy_concern",
        },
    )

    assert accepted.status_code == 200
    assert accepted.json()["feedback_id"].startswith("fb_")
    assert rejected.status_code == 403
    assert len(feedback_store.records) == 1


def test_beta_evaluation_export_is_redacted_and_aggregated(monkeypatch) -> None:
    runtime = AgentGraphRuntime(memory_store=InMemoryStore(), trace_store=InMemoryTraceStore())
    feedback_store = InMemoryBetaFeedbackStore()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_beta_feedback_store", lambda: feedback_store)
    client = TestClient(create_app())

    run = client.post("/agent/run", json={"user_id": "u1", "session_id": "s1", "text": "帮我写介绍"}).json()
    client.post(
        "/beta/feedback",
        json={
            "run_id": run["run_id"],
            "user_id": "u1",
            "rating": "down",
            "category": "wrong_answer",
            "note": "不要泄露 secret_token=abc",
        },
    )

    export = client.get("/beta/evaluations", params={"user_id": "u1"})
    payload = export.json()

    assert export.status_code == 200
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["down"] == 1
    assert payload["summary"]["by_category"]["wrong_answer"] == 1
    assert payload["items"][0]["run"]["run_id"] == run["run_id"]
    assert "secret_token=abc" not in export.text


def test_beta_delete_user_data_clears_only_target_user(monkeypatch) -> None:
    memory_store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=memory_store, trace_store=InMemoryTraceStore())
    feedback_store = InMemoryBetaFeedbackStore()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_beta_feedback_store", lambda: feedback_store)
    client = TestClient(create_app())

    memory_store.save(_memory("m1", "u1"))
    memory_store.save(_memory("m2", "u2"))
    run_u1 = client.post("/agent/run", json={"user_id": "u1", "session_id": "s1", "text": "找相似款"}).json()
    run_u2 = client.post("/agent/run", json={"user_id": "u2", "session_id": "s1", "text": "找相似款"}).json()
    client.post("/beta/feedback", json={"run_id": run_u1["run_id"], "user_id": "u1", "rating": "down"})
    client.post("/beta/feedback", json={"run_id": run_u2["run_id"], "user_id": "u2", "rating": "up"})

    deleted = client.delete("/beta/users/u1/data")

    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["memory_items"] >= 1
    assert deleted.json()["deleted"]["session_records"] >= 1
    assert not runtime.trace_store.list_by_user("u1")
    assert runtime.trace_store.list_by_user("u2")
    assert runtime.session_store.list_by_user("u1") == []
    assert runtime.session_store.list_by_user("u2")
    assert feedback_store.list_by_user("u1") == []
    assert len(feedback_store.list_by_user("u2")) == 1
    assert memory_store.list_by_user("u1") == []
    assert memory_store.list_by_user("u2")


def _memory(memory_id: str, user_id: str) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        memory_type="preference",
        summary=f"{user_id} 的偏好",
        created_at=datetime.now(timezone.utc),
    )
