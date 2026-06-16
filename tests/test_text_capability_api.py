from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_direct_chat_api_returns_capability_contract_without_tools() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "解释一下 Agent 和 Tool 的区别"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "direct_chat"
    assert payload["tool_calls"] == []
    assert payload["tool_results"] == []
    assert payload["errors"] == []

    data = payload["data"]
    assert data["provider"] == "mock"
    assert data["contract"]["capability"] == "direct_chat"
    assert data["contract"]["status"] == "succeeded"
    assert data["contract"]["output_ref"] == "mock://chat/direct"


def test_text_image_generation_api_returns_tool_result_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "生成一张赛博朋克风格海报"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "image_generation"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["image_generation"]
    assert payload["errors"] == []

    tool_result = payload["tool_results"][0]
    assert tool_result["success"] is True
    assert tool_result["data"]["provider"] == "mock"
    assert tool_result["data"]["image_url"] == "local://generated/poster.png"
    assert tool_result["data"]["contract"]["capability"] == "image_generation"
    assert tool_result["data"]["contract"]["status"] == "succeeded"
    assert tool_result["data"]["contract"]["output_ref"] == "local://generated/poster.png"
    assert payload["data"]["image_url"] == "local://generated/poster.png"
