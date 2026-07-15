from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_web_search_api_returns_mock_search_contract_for_latest_news_request() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "查一下今天 AI 行业最新消息",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "web_search"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["web_search"]
    assert payload["errors"] == []

    result = payload["tool_results"][0]
    assert result["success"] is True
    assert result["data"]["provider"] == "mock"
    assert result["data"]["query_used"] == "查一下今天 AI 行业最新消息"
    assert result["data"]["results"][0]["url"].startswith("mock://web-search/")
