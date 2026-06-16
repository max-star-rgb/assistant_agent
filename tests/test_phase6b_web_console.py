from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_demo_console_page_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")

    assert response.status_code == 200
    assert "Assistant Demo Console" in response.text
    assert "fetch(\"/demo/scenarios\")" in response.text
    assert "fetch(\"/agent/run\"" in response.text


def test_static_console_asset_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "Demo scenario" in response.text
    assert "Trace" in response.text
