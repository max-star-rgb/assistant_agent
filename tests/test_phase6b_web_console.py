from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_demo_console_page_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")

    assert response.status_code == 200
    assert "Assistant Chat" in response.text
    assert 'fetchJson("/agent/run"' in response.text


def test_static_console_asset_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "Examples" in response.text
    assert "Conversation History" in response.text
    assert "Assistant ReAct Process" in response.text
