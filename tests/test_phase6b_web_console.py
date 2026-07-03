from fastapi.testclient import TestClient

import assistant_agent.api.app as app_module
from assistant_agent.api.app import create_app


def test_demo_console_page_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")

    assert response.status_code == 200
    assert "Assistant Chat" in response.text
    assert "请输入你的工号：00xxxx" in response.text
    assert "new WebSocket" in response.text
    assert "/ws/agent/" in response.text
    assert "/ws/realtime/media" in response.text


def test_static_console_asset_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "Examples" in response.text
    assert "Conversation History" in response.text
    assert "Product Results" in response.text
    assert "Generated Images" in response.text
    assert "Assistant ReAct Process" in response.text
    assert "Entry" in response.text
    assert "App + Media Relay" in response.text
    assert "Start Call" in response.text
    assert "Hang Up" in response.text
    assert "Cancel Agent" in response.text
    assert "Ping Gateway" in response.text
    assert "Send Media Event" in response.text
    assert "Interrupt With Text" in response.text
    assert "app-media-entry-tab" in response.text
    assert "app-media-panel" in response.text
    assert "realtime-connect" in response.text
    assert "realtime-cancel" in response.text
    assert "realtime-ping" in response.text
    assert "sendMediaEvent" in response.text
    assert "/ws/realtime/media" in response.text
    assert "session.start" in response.text
    assert "transcript.final" in response.text
    assert "run.cancel" in response.text
    assert "session.end" in response.text
    assert "/ws/gateway" not in response.text
    assert "Realtime Media Relay" not in response.text
    assert "realtime-mode-tab" not in response.text
    assert "realtime-panel" not in response.text
    assert "data-experience-mode" not in response.text
    assert "/demo/access" in response.text
    assert "/demo/examples" in response.text
    assert "renderExamples" in response.text
    assert "validateTrialUserId" in response.text
    assert "collectProductResults" in response.text
    assert "renderProductGallery" in response.text
    assert "collectImageArtifacts" in response.text
    assert "normalizeArtifactUrl" in response.text
    assert "Control Plane" in response.text
    assert "Pilot Readiness" in response.text
    assert "Recent Runs / Audit" in response.text
    assert "Gateway Route" in response.text
    assert "Delegation Tree" in response.text
    assert "Redaction Status" in response.text
    assert "control-plane-run-id" in response.text
    assert "control-plane-trace-id" in response.text
    assert "/control-plane/readiness" in response.text
    assert "/control-plane/audit/events?limit=12" in response.text
    assert "/control-plane/runs/" in response.text
    assert "refreshControlPlaneReadiness" in response.text
    assert "refreshControlPlaneRun" in response.text
    assert "refreshControlPlaneRecent" in response.text
    assert "remote control" not in response.text.lower()
    assert "provider toggle" not in response.text.lower()


def test_generated_artifact_static_path_is_served(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "GENERATED_ARTIFACT_DIR", tmp_path)
    (tmp_path / "sample.png").write_bytes(b"fake-png")
    client = TestClient(create_app())

    response = client.get("/artifacts/generated/sample.png")

    assert response.status_code == 200
    assert response.content == b"fake-png"
    assert response.headers["content-type"] == "image/png"


def test_demo_examples_endpoint_serves_shared_examples() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/examples")
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert "帮我购买乐事薯片，先搜索商品并比较价格，给出购买建议。" in payload["examples"]
