from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.config import ProviderConfig


def test_success_response_includes_protocol_version_and_trace_id() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")
    assert payload["errors"] == []


def test_invalid_request_error_uses_stable_error_shape() -> None:
    client = TestClient(create_app())

    response = client.post("/agent/run", json={"session_id": "s1", "text": "你好"})

    payload = response.json()
    assert response.status_code == 422
    assert payload["protocol_version"] == "v1"
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "INVALID_REQUEST"
    assert payload["errors"][0]["message"]
    assert payload["errors"][0]["detail"]["fields"]
    assert payload["errors"][0]["recoverable"] is True


def test_provider_unconfigured_error_uses_stable_error_shape(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_agent,
        "get_agent_runtime",
        lambda: AgentGraphRuntime(config=ProviderConfig(vision_provider="openai", openai_api_key=None)),
    )
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "图里是什么", "image_ids": ["img1"]},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "PROVIDER_UNCONFIGURED"
    assert payload["errors"][0]["message"]
    assert payload["errors"][0]["detail"]["source"] == "vision_understanding"
    assert payload["errors"][0]["recoverable"] is False
