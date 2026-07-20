from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_shopping_search_api_returns_offer_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找 500 元以内的白鞋，再比较价格"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "multi_step_orchestration"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["shopping_search"]
    assert payload["errors"] == []

    compare_result = payload["tool_results"][0]
    assert compare_result["success"] is True
    assert compare_result["data"]["provider"] == "mock"
    assert compare_result["data"]["offers"][0]["product_id"] == "p2"
    assert compare_result["data"]["best_offer"]["product_id"] == "p2"
    assert compare_result["data"]["ranking_reason"]["explanation"]
    assert "provider_response" not in compare_result["data"]
    assert "raw" not in compare_result["data"]


def test_shopping_search_api_without_products_returns_structured_error_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "哪个平台更便宜"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["intent"] == "shopping_search"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["shopping_search"]

    result = payload["tool_results"][0]
    assert result["success"] is False
    assert result["data"]["provider"] == "mock"
    assert result["data"]["errors"][0]["code"] == "price_no_products"
    assert payload["errors"][0]["detail"]["source"] == "shopping_search"
