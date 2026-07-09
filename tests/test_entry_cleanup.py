from pathlib import Path

from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_legacy_web_chat_console_is_not_served() -> None:
    client = TestClient(create_app())

    assert client.get("/demo/console").status_code == 404
    assert client.get("/static/index.html").status_code == 404


def test_legacy_ws_agent_route_and_module_are_removed() -> None:
    app = create_app()

    route_paths = {getattr(route, "path", "") for route in app.routes}

    assert "/ws/agent/{session_id}" not in route_paths
    assert not Path("src/assistant_agent/api/websocket.py").exists()


def test_legacy_remote_web_chat_client_is_removed() -> None:
    assert not Path("scripts/run_client.py").exists()


def test_realtime_runtime_entries_remain_registered() -> None:
    app = create_app()

    route_paths = {getattr(route, "path", "") for route in app.routes}

    assert "/agent/run" in route_paths
    assert "/ws/gateway" in route_paths
    assert "/ws/realtime/media" in route_paths
    assert "/agent-service/{version}" in route_paths
    assert "/artifacts/generated" in route_paths


def test_run_server_help_points_to_realtime_entries() -> None:
    source = Path("scripts/run_server.py").read_text(encoding="utf-8")

    assert "/demo/console" not in source
    assert "scripts/run_client.py" not in source
    assert "scripts/realtime_media_client.py" in source
    assert "scripts/run_gateway_client.py" in source
