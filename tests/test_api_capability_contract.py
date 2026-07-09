from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_api_tool_results_include_capability_contracts() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找 500 元以内的白鞋，再比较价格"},
    )

    assert response.status_code == 200
    payload = response.json()
    contracts = [result["contract"] for result in payload["tool_results"]]

    assert [contract["capability"] for contract in contracts] == ["product_search", "price_compare"]
    assert all(contract["status"] == "succeeded" for contract in contracts)
    assert payload["data"]["contracts"][0]["capability"] == "product_search"


def test_api_direct_chat_includes_capability_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我写一段商品介绍"},
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["tool_results"] == []
    assert payload["data"]["contract"]["capability"] == "direct_chat"
    assert payload["data"]["contract"]["status"] == "succeeded"


def test_api_failed_tool_contract_has_errors() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "哪个便宜"},
    )

    assert response.status_code == 200
    payload = response.json()
    contract = payload["tool_results"][0]["contract"]

    assert payload["status"] == "failed"
    assert contract["capability"] == "price_compare"
    assert contract["status"] == "failed"
    assert contract["errors"][0]["code"]
    assert contract["errors"][0]["message"]
