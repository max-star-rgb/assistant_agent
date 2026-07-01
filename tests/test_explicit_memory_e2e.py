import json

from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.config import ProviderConfig


def test_explicit_memory_save_survives_runtime_restart_and_snapshot_recall(tmp_path, monkeypatch) -> None:
    memory_path = tmp_path / "memories.jsonl"
    conversation_path = tmp_path / "conversation_history.jsonl"
    config = ProviderConfig(
        memory_backend="jsonl",
        memory_path=str(memory_path),
        conversation_history_backend="jsonl",
        conversation_history_path=str(conversation_path),
    )
    runtime_holder = {"runtime": AgentGraphRuntime(config=config)}
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime_holder["runtime"])
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "记住我爱玉桂狗"},
    )

    assert run_response.status_code == 200
    assert memory_path.exists()
    persisted = [
        item
        for item in [json.loads(line) for line in memory_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if "memory_id" in item
    ]
    assert {item["source"] for item in persisted} == {"explicit_user_request", "user_profile"}
    explicit = next(item for item in persisted if item["source"] == "explicit_user_request")
    profile = next(item for item in persisted if item["source"] == "user_profile")
    assert explicit["memory_type"] == "preference"
    assert explicit["summary"] == "我爱玉桂狗"
    assert profile["memory_id"] == "user_profile"
    assert "我爱玉桂狗" in profile["summary"]

    runtime_holder["runtime"] = AgentGraphRuntime(config=config)
    snapshot_response = client.get(
        "/memory/users/u1/snapshot",
        params={"session_id": "s1", "query": "玉桂狗", "include_content": True},
    )

    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    assert snapshot["session"]["thread_id"] == "s1"
    assert snapshot["conversation_history"]["total"] == 1
    assert snapshot["audit"]["profile_present"] is True
    assert snapshot["memory_context"]["total"] == 2
    recalled_items = [
        item
        for block in snapshot["memory_context"]["blocks"]
        for item in block["items"]
    ]
    assert {item["source"] for item in recalled_items} == {"explicit_user_request", "user_profile"}
    assert {item["memory_type"] for item in recalled_items} == {"preference"}
    assert all("玉桂狗" in item["summary"] for item in recalled_items)
