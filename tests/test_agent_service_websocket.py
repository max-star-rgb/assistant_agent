from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant_agent.agent.state import AgentState
from assistant_agent.api import routes_agent
from assistant_agent.api import agent_service_websocket as agent_service_ws
from assistant_agent.api.app import create_app
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoFrame


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []
        self.video_context_store = InMemoryVideoContextStore()

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_agent_service_gateway_test")
        state.set_response(AgentResponse(message="agent service gateway response"))
        return state


def test_agent_service_start_ack_accepts_media_envelope() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {
                    "userInfo": {"number": "10086"},
                    "agentInfo": {"agentNumber": "9001"},
                    "optional": {"kept": True},
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert response["sessionId"] == "s1"
    assert _body(response) == {"code": "OK"}


def test_agent_service_chat_returns_mock_chat_response() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert response["sessionId"] == "s1"
    body = _body(response)
    assert body["number"] == "10086"
    assert body["message"]["chatIndex"] == 2
    assert "你好" in body["message"]["content"]


def test_agent_service_chat_runs_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    body = _body(response)
    assert response["message"] == "chatResponse"
    assert response["sessionId"] == "s1"
    assert body["number"] == "10086"
    assert body["message"]["chatIndex"] == 2
    assert body["message"]["content"] == "agent service gateway response"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "10086"
    assert request.session_id == "s1"
    assert request.text == "你好"
    assert request.metadata["runtime"]["history"] == ["你好"]
    assert request.metadata["transport"] == "agent_service_websocket"
    assert request.metadata["agent_service"]["chat_index"] == 2
    assert request.metadata["runtime"]["entry_capabilities"] == {
        "supports_text_streaming": False,
        "supports_interrupt": False,
        "supports_tts_state": False,
        "supports_realtime_task_state": True,
        "supports_audio_refs": False,
        "supports_image_refs": False,
        "supports_video_refs": True,
        "supports_raw_media": True,
        "supports_tts_edge_events": False,
    }


def test_agent_service_video_context_reaches_following_chat(monkeypatch, tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)

    class FakeVideoIngestion:
        def __init__(self) -> None:
            self.cleaned: list[str] = []

        def ingest(self, session_id, frame_index, video_hex, video_config, timestamp):
            assert session_id == "10086"
            assert frame_index == "video-1"
            assert video_hex == "0000000165aa"
            assert video_config["codec"] == "H264"
            assert timestamp == "2026-07-13T08:30:00Z"
            return VideoFrame(
                video_id="agent-service-video-test",
                frame_id="frame-1",
                uri=str(tmp_path / "frame-1.jpg"),
                sequence=1,
            )

        def cleanup(self, video_id: str) -> None:
            self.cleaned.append(video_id)

    ingestion = FakeVideoIngestion()
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", lambda: ingestion)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope("assistantControl", {"number": "10086", "callType": "VIDEO"})
        )
        websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "0000000165aa",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264", "resolution": "1280x720", "frameRate": 25},
                },
            )
        )
        video_response = websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-video-1",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "识别眼前物体",
                            "time": "2026-07-13T08:30:01Z",
                        }
                    ],
                },
            )
        )
        websocket.receive_json()

    assert _body(video_response) == {"code": 0, "message": "video received"}
    assert runtime.requests[0].video_ids == ["agent-service-video-test"]
    capabilities = runtime.requests[0].metadata["runtime"]["entry_capabilities"]
    assert capabilities["supports_video_refs"] is True
    assert capabilities["supports_raw_media"] is True
    assert ingestion.cleaned == ["agent-service-video-test"]


def test_agent_service_accepts_media_control_and_chat_protocol(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "assistantControl",
                {
                    "number": "10086",
                    "callType": "AUDIO",
                    "modelName": "mock-model",
                },
            )
        )
        control_response = websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-1",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-09T10:00:00+08:00",
                        },
                        {
                            "speakerNumber": "10086",
                            "imageContent": "aW1hZ2U=",
                            "time": "2026-07-09T10:00:01+08:00",
                        }
                    ],
                    "stream": True,
                },
            )
        )
        chat_response = websocket.receive_json()

    assert control_response["message"] == "assistantControl"
    assert "sessionId" not in control_response
    assert _body(control_response) == {
        "code": 0,
        "message": "success",
        "phoneNumber": "10086",
    }

    assert chat_response["message"] == "chatResponse"
    assert "sessionId" not in chat_response
    body = _body(chat_response)
    assert body == {
        "message": {
            "chatIndex": "chat-1",
            "content": {
                "intentResult": {
                    "description": "agent service gateway response",
                    "status": "SUCCESS",
                }
            },
        },
        "display_only": False,
    }
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "10086"
    assert request.session_id == "10086"
    assert request.text == "你好"
    assert request.metadata["transport"] == "agent_service_websocket"
    assert request.metadata["agent_service"]["chat_index"] == "chat-1"


def test_agent_service_accepts_media_audio_video_and_interrupt_protocol(monkeypatch, tmp_path: Path) -> None:
    class AcceptingVideoIngestion:
        def ingest(self, *_args):
            return VideoFrame(
                video_id="agent-service-video-protocol",
                frame_id="frame-1",
                uri=str(tmp_path / "frame-1.jpg"),
                sequence=1,
            )

        def cleanup(self, _video_id: str) -> None:
            return None

    monkeypatch.setattr(
        agent_service_ws,
        "_create_video_ingestion_service",
        lambda: AcceptingVideoIngestion(),
    )
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "assistantControl",
                {
                    "number": "10086",
                    "callType": "VIDEO",
                },
            )
        )
        websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "audio",
                {
                    "userNumber": "10086",
                    "audioIndex": "audio-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "audioContent": "00ff",
                            "time": "2026-07-09T10:00:01+08:00",
                        }
                    ],
                    "audioConfig": {"codec": "opus", "sampleRate": 16000, "channels": 1},
                },
            )
        )
        audio_response = websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "00ff",
                            "time": "2026-07-09T10:00:02+08:00",
                        }
                    ],
                    "videoConfig": {"codec": "H264", "resolution": "1280x720", "frameRate": 30},
                },
            )
        )
        video_response = websocket.receive_json()

        websocket.send_json(_media_envelope("interrupt", {"number": "10086"}))
        interrupt_response = websocket.receive_json()

    assert audio_response["message"] == "audioResponse"
    assert "sessionId" not in audio_response
    assert _body(audio_response) == {"code": 0, "message": "audio received"}

    assert video_response["message"] == "videoResponse"
    assert "sessionId" not in video_response
    assert _body(video_response) == {"code": 0, "message": "video received"}

    assert interrupt_response["message"] == "interrupt"
    assert "sessionId" not in interrupt_response
    assert _body(interrupt_response) == {"code": 0, "message": "interrupted"}


def test_agent_service_rejects_invalid_video_and_keeps_connection_usable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    ingestion = agent_service_ws.H264VideoIngestionService(
        store=runtime.video_context_store,
        root=tmp_path,
        decoder=lambda *_: None,
    )
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", lambda: ingestion)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "bad-video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "00ff",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264"},
                },
            )
        )
        video_response = websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-after-bad-video",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-13T08:30:01Z",
                        }
                    ],
                },
            )
        )
        chat_response = websocket.receive_json()

    assert _body(video_response) == {
        "code": "FAIL",
        "message": "videoContent must use an Annex-B NAL start code",
    }
    assert _body(chat_response)["message"]["content"]["intentResult"]["status"] == "SUCCESS"
    assert runtime.requests[0].video_ids == []


def test_agent_service_validates_required_start_fields() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {"userInfo": {}, "agentInfo": {"agentNumber": "9001"}},
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "userInfo.number" in _body(response)["message"]


def test_agent_service_validates_chat_contents() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 1,
                    "userNumber": "10086",
                    "contents": [{"speakerNumber": "10086", "speechContent": "hello"}],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert _body(response)["code"] == "FAIL"
    assert "contents[0].time" in _body(response)["message"]


def test_agent_service_rejects_malformed_body_and_unknown_message() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json({"message": "chat", "sessionId": "s1", "body": {"not": "a string"}})
        malformed = websocket.receive_json()

        websocket.send_json(_envelope("notSupported", "s1", {}))
        unknown = websocket.receive_json()

    assert malformed["message"] == "chatResponse"
    assert _body(malformed)["code"] == "FAIL"
    assert "body must be a JSON string" in _body(malformed)["message"]
    assert unknown["message"] == "error"
    assert _body(unknown)["code"] == "FAIL"
    assert "unknown message type" in _body(unknown)["message"]


def test_agent_service_requires_session_id() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            {
                "message": "assistantControlStart",
                "body": json.dumps(
                    {"userInfo": {"number": "10086"}, "agentInfo": {"agentNumber": "9001"}},
                    ensure_ascii=False,
                ),
            }
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "sessionId" in _body(response)["message"]


def test_agent_service_rejects_non_v1_path() -> None:
    client = TestClient(create_app())

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/agent-service/v2?sessionId=s1") as websocket:
            response = websocket.receive_json()
            assert response["message"] == "error"
            assert _body(response)["code"] == "FAIL"
            assert "unsupported agent service version" in _body(response)["message"]
            websocket.receive_json()

    assert exc_info.value.code == 1008


def _envelope(message: str, session_id: str, body: dict) -> dict:
    return {
        "message": message,
        "sessionId": session_id,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _media_envelope(message: str, body: dict) -> dict:
    return {
        "message": message,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _body(response: dict) -> dict:
    return json.loads(response["body"])
