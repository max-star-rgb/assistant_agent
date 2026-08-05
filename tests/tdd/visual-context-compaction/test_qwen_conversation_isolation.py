from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.providers.qwen_realtime_vision import (
    QwenRealtimeVisionAdapter,
    QwenRealtimeVisionConfig,
)


class FakeSocket:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = [json.dumps(event) for event in events]
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, *, timeout: float) -> str:
        assert timeout > 0
        return self._events.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, *, completed: bool = True) -> None:
        self.completed = completed
        self.sockets: list[FakeSocket] = []

    def __call__(self, *_: object, **__: object) -> FakeSocket:
        observation_events = [
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {
                "type": "response.text.delta",
                "delta": json.dumps({"summary": "current frame"}),
            },
            {
                "type": "response.done",
                "response": {"status": "completed" if self.completed else "failed"},
            },
        ]
        socket = FakeSocket(
            [
                {"type": "session.created"},
                {"type": "session.updated"},
                *observation_events,
                # A reused socket can still finish the old implementation's
                # second call, so the regression fails on isolation itself.
                *observation_events,
            ]
        )
        self.sockets.append(socket)
        return socket


def _jpeg(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8offline-jpeg\xff\xd9")
    return path


def _request(frame: Path, *, sequence: int) -> VideoUnderstandingRequest:
    return VideoUnderstandingRequest(
        video_ref="video-1",
        frame_refs=[str(frame)],
        user_query="update current frame",
        metadata={"frame_sequence": sequence},
    )


def test_each_qwen_observation_uses_fresh_conversation_and_one_jpeg(
    tmp_path: Path,
) -> None:
    connector = FakeConnector()
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="offline-test-key"),
        connect=connector,
    )

    first = adapter.understand_video(
        _request(_jpeg(tmp_path, "frame-1.jpg"), sequence=1)
    )
    second = adapter.understand_video(
        _request(_jpeg(tmp_path, "frame-2.jpg"), sequence=2)
    )

    assert first.errors == []
    assert second.errors == []
    assert len(connector.sockets) == 2
    assert all(socket.closed for socket in connector.sockets)
    assert [
        sum(event["type"] == "input_image_buffer.append" for event in socket.sent)
        for socket in connector.sockets
    ] == [1, 1]
    assert adapter.last_observation_diagnostics["connection_reused"] is False
    assert adapter.last_observation_diagnostics["session_generation"] == 2


def test_incomplete_qwen_observation_discards_its_conversation(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(completed=False)
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="offline-test-key"),
        connect=connector,
    )

    result = adapter.understand_video(
        _request(_jpeg(tmp_path, "frame-failed.jpg"), sequence=1)
    )

    assert result.errors[0]["code"] == "provider_incomplete_response"
    assert len(connector.sockets) == 1
    assert connector.sockets[0].closed is True
