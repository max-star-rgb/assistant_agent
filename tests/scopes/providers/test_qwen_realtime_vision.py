import base64
import json
from pathlib import Path
from typing import Any

from assistant_agent.providers.qwen_realtime_vision import (
    QwenRealtimeVisionAdapter,
    QwenRealtimeVisionConfig,
)
from assistant_agent.schemas.perception import VideoUnderstandingRequest


class FakeWebSocket:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = [json.dumps(item) for item in responses]
        self.sent: list[dict] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, timeout: float | None = None) -> str:
        _ = timeout
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _frame(tmp_path: Path, name: str = "frame.jpg", body: bytes = b"jpeg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8" + body + b"\xff\xd9")
    return path


def _successful_responses(summary: str = "ok") -> list[dict[str, Any]]:
    return [
        {"type": "session.created"},
        {"type": "session.updated"},
        {"type": "response.text.delta", "delta": json.dumps({"summary": summary})},
        {"type": "response.done"},
    ]


def test_realtime_adapter_handshake_and_single_frame_protocol(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "response.text.delta", "delta": '{"summary":"杯子在桌上",'},
            {"type": "response.text.delta", "delta": '"objects":["杯子"]}'},
            {"type": "response.done"},
        ]
    )
    connect_calls: list[tuple[str, dict]] = []

    def connect(url: str, **kwargs):
        connect_calls.append((url, kwargs))
        return socket

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key", model="qwen-realtime-test"),
        connect=connect,
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(
            video_ref="video-1",
            frame_refs=[str(frame)],
            user_query="描述当前画面",
            memory_context="上一轮没有杯子",
        )
    )

    assert result.errors == []
    assert result.summary == "杯子在桌上"
    assert result.objects == ["杯子"]
    assert connect_calls[0][0].endswith("?model=qwen-realtime-test")
    assert connect_calls[0][1]["additional_headers"]["Authorization"] == "Bearer test-key"
    assert [event["type"] for event in socket.sent] == [
        "session.update",
        "input_audio_buffer.append",
        "input_image_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]
    silence = base64.b64decode(socket.sent[1]["audio"])
    assert len(silence) == 24_000 * 2 // 5
    assert socket.sent[2]["image"].startswith("data:image/jpeg;base64,")
    instructions = socket.sent[4]["response"]["instructions"]
    assert "描述当前画面" in instructions
    assert "上一轮没有杯子" in instructions


def test_realtime_adapter_rejects_non_single_or_oversized_frame_without_connecting(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    oversized = _frame(tmp_path, "large.jpg", bytes(200_000))
    connected = False

    def connect(*_args, **_kwargs):
        nonlocal connected
        connected = True

    adapter = QwenRealtimeVisionAdapter(QwenRealtimeVisionConfig(api_key="test-key"), connect=connect)

    no_frame = adapter.understand_video(VideoUnderstandingRequest(video_ref="v", frame_refs=[]))
    too_many = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame), str(frame)])
    )
    too_large = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(oversized)])
    )

    assert no_frame.errors[0]["code"] == "invalid_frame_count"
    assert too_many.errors[0]["code"] == "invalid_frame_count"
    assert too_large.errors[0]["code"] == "frame_too_large"
    assert connected is False


def test_realtime_adapter_sanitizes_invalid_json_and_closes_failed_connection(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "response.text.delta", "delta": "secret-provider-garbage"},
            {"type": "response.done"},
        ]
    )
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"), connect=lambda *_args, **_kwargs: socket
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == [
        {
            "code": "provider_bad_response",
            "message": "Qwen realtime vision request failed.",
            "recoverable": True,
        }
    ]
    assert "secret-provider-garbage" not in result.model_dump_json()
    assert socket.closed is True


def test_realtime_adapter_maps_timeout_and_disconnect_to_structured_errors(tmp_path: Path) -> None:
    frame = _frame(tmp_path)

    class TimeoutSocket(FakeWebSocket):
        def recv(self, timeout: float | None = None) -> str:
            if self.responses:
                return super().recv(timeout)
            raise TimeoutError("raw timeout details")

    timeout_socket = TimeoutSocket(
        [{"type": "session.created"}, {"type": "session.updated"}]
    )
    timeout_adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: timeout_socket,
    )
    disconnect_adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("secret disconnect")),
    )
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    timeout = timeout_adapter.understand_video(request)
    disconnected = disconnect_adapter.understand_video(request)

    assert timeout.errors[0]["code"] == "provider_timeout"
    assert timeout_socket.closed is True
    assert disconnected.errors[0]["code"] == "provider_connection_failed"
    assert "secret disconnect" not in disconnected.model_dump_json()


def test_realtime_adapter_maps_mid_response_disconnect_without_raw_details(tmp_path: Path) -> None:
    frame = _frame(tmp_path)

    class DisconnectSocket(FakeWebSocket):
        def recv(self, timeout: float | None = None) -> str:
            if self.responses:
                return super().recv(timeout)
            raise ConnectionError("raw mid-response secret")

    socket = DisconnectSocket(
        [{"type": "session.created"}, {"type": "session.updated"}]
    )
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: socket,
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors[0]["code"] == "provider_connection_failed"
    assert "raw mid-response secret" not in result.model_dump_json()
    assert socket.closed is True


def test_realtime_adapter_uses_capped_backoff_and_resets_after_success(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    sleeps: list[float] = []
    attempts = 0
    success_socket = FakeWebSocket(_successful_responses())

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            raise ConnectionError("offline")
        return success_socket

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"), connect=connect, sleep=sleeps.append
    )
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    for _ in range(6):
        assert adapter.understand_video(request).errors[0]["code"] == "provider_connection_failed"
    assert adapter.understand_video(request).errors == []

    assert sleeps == [0.25, 0.5, 1.0, 2.0, 5.0, 5.0]
    assert adapter.connection_failures == 0


def test_realtime_adapter_rotates_after_twenty_successes_and_closes(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    sockets = [
        FakeWebSocket(
            [
                {"type": "session.created"},
                {"type": "session.updated"},
                *sum(
                    ([{"type": "response.text.delta", "delta": '{"summary":"ok"}'}, {"type": "response.done"}] for _ in range(20)),
                    [],
                ),
            ]
        ),
        FakeWebSocket(_successful_responses("rotated")),
    ]
    connect_count = 0

    def connect(*_args, **_kwargs):
        nonlocal connect_count
        socket = sockets[connect_count]
        connect_count += 1
        return socket

    adapter = QwenRealtimeVisionAdapter(QwenRealtimeVisionConfig(api_key="test-key"), connect=connect)
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    for _ in range(20):
        assert adapter.understand_video(request).errors == []
    assert adapter.understand_video(request).summary == "rotated"
    adapter.close()

    assert connect_count == 2
    assert sockets[0].closed is True
    assert sockets[1].closed is True


def test_realtime_adapter_rotates_connection_after_sixty_seconds(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    now = [10.0]
    sockets = [
        FakeWebSocket(_successful_responses("first")),
        FakeWebSocket(_successful_responses("second")),
    ]

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: sockets.pop(0),
        clock=lambda: now[0],
    )
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    assert adapter.understand_video(request).summary == "first"
    first = adapter._socket
    now[0] = 70.0
    assert adapter.understand_video(request).summary == "second"

    assert first.closed is True
