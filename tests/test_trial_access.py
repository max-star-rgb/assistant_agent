from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app
from multimodal_agent.services.trial_access import (
    TRIAL_USER_ID_FILE_ENV,
    TRIAL_USER_IDS_ENV,
    trial_access_gate_from_env,
)


def test_trial_access_open_mode_allows_non_empty_user_id() -> None:
    gate = trial_access_gate_from_env({})

    allowed = gate.check(" alice ")
    missing = gate.check("")

    assert gate.access_required is False
    assert allowed.allowed is True
    assert allowed.user_id == "alice"
    assert missing.allowed is False


def test_trial_access_reads_env_and_file(tmp_path) -> None:
    users_file = tmp_path / "trial-users.txt"
    users_file.write_text("bob\ncarol, phone demo # comment\n", encoding="utf-8")

    gate = trial_access_gate_from_env(
        {
            TRIAL_USER_IDS_ENV: "alice",
            TRIAL_USER_ID_FILE_ENV: str(users_file),
        }
    )

    assert gate.access_required is True
    assert gate.allowed_user_ids == frozenset({"alice", "bob", "carol", "phone_demo"})
    assert gate.check("phone demo").allowed is True
    assert gate.check("unknown").allowed is False


def test_demo_access_endpoint_reports_allowed_status(monkeypatch) -> None:
    monkeypatch.setenv(TRIAL_USER_IDS_ENV, "pilot_01")
    client = TestClient(create_app())

    allowed = client.get("/demo/access", params={"user_id": "pilot_01"})
    rejected = client.get("/demo/access", params={"user_id": "unknown"})

    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True
    assert allowed.json()["access_required"] is True
    assert rejected.status_code == 200
    assert rejected.json()["allowed"] is False
    assert rejected.json()["reason"]


def test_agent_run_rejects_unlisted_trial_user(monkeypatch) -> None:
    monkeypatch.setenv(TRIAL_USER_IDS_ENV, "pilot_01")
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "unknown", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 403
    assert "试用名单" in response.json()["detail"]


def test_websocket_rejects_unlisted_trial_user(monkeypatch) -> None:
    monkeypatch.setenv(TRIAL_USER_IDS_ENV, "pilot_01")
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=你好&user_id=unknown") as websocket:
        event = websocket.receive_json()

    assert event["type"] == "agent_error"
    assert event["error"]["code"] == "ACCESS_DENIED"
    assert "试用名单" in event["error"]["message"]


def test_local_cli_websocket_can_bypass_trial_access(monkeypatch) -> None:
    monkeypatch.setenv(TRIAL_USER_IDS_ENV, "pilot_01")
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=你好&user_id=unknown&client=cli") as websocket:
        final = _receive_until(websocket, "agent_response")

    assert final["payload"]["response"]["status"] == "completed"


def _receive_until(websocket, event_type: str, limit: int = 20) -> dict:
    for _ in range(limit):
        event = websocket.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"did not receive {event_type}")
