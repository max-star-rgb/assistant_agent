from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_http_agent_run_uses_graph_runtime_and_exposes_tool_observability() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "shopping_search"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["shopping_search"]
    assert payload["tool_results"][0]["success"] is True
    assert payload["errors"] == []


def test_http_agent_run_returns_structured_tool_error() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "哪个便宜"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["intent"] == "shopping_search"
    assert payload["tool_calls"][0]["status"] == "failed"
    assert payload["tool_results"][0]["success"] is False
    assert payload["errors"][0]["code"] == "TOOL_INPUT_INVALID"
    assert payload["errors"][0]["detail"]["source"] == "shopping_search"
