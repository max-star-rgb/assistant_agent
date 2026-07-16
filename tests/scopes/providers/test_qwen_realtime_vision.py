import base64
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from assistant_agent.providers import qwen_realtime_vision as qwen_realtime
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
        {"type": "session.updated"},
        {"type": "input_audio_buffer.committed"},
        {"type": "response.text.delta", "delta": json.dumps({"summary": summary})},
        {"type": "response.done", "response": {"status": "completed"}},
    ]


def test_realtime_transport_imports_without_injected_connector() -> None:
    transport = importlib.import_module("websockets.sync.client")

    assert callable(transport.connect)
    assert QwenRealtimeVisionAdapter(QwenRealtimeVisionConfig())._connect is qwen_realtime._default_connect


def test_default_connect_bounds_close_handshake(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def connect(url: str, **kwargs: Any) -> object:
        captured.update({"url": url, **kwargs})
        return sentinel

    transport = importlib.import_module("websockets.sync.client")
    monkeypatch.setattr(transport, "connect", connect)

    result = qwen_realtime._default_connect("wss://qwen.local/realtime", open_timeout=2.0)

    assert result is sentinel
    assert captured == {
        "url": "wss://qwen.local/realtime",
        "open_timeout": 2.0,
        "close_timeout": 1.0,
    }


def test_realtime_adapter_handshake_and_single_frame_protocol(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {"type": "response.text.delta", "delta": '{"summary":"杯子在桌上",'},
            {"type": "response.text.delta", "delta": '"objects":["杯子"]}'},
            {"type": "response.done", "response": {"status": "completed"}},
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
        "session.update",
        "input_audio_buffer.append",
        "input_image_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert socket.sent[0]["session"]["input_audio_format"] == "pcm"
    assert socket.sent[0]["session"]["output_audio_format"] == "pcm"
    assert socket.sent[0]["session"]["turn_detection"] is None
    silence = base64.b64decode(socket.sent[2]["audio"])
    assert len(silence) == 6_400
    assert base64.b64decode(socket.sent[3]["image"]) == frame.read_bytes()
    assert not socket.sent[3]["image"].startswith("data:")
    instructions = socket.sent[1]["session"]["instructions"]
    assert "描述当前画面" in instructions
    assert "上一轮没有杯子" in instructions
    assert "角色: 实时视觉理解器" in instructions
    assert "技能:" in instructions
    assert "规则:" in instructions
    assert "工作流程:" in instructions
    assert "video_understanding" not in instructions
    assert "tool_calls" not in instructions
    assert "provider-native" not in instructions
    assert "```" not in instructions
    assert socket.sent[5] == {"type": "response.create"}


def test_realtime_adapter_reports_prompt_safe_session_and_sequence_diagnostics(
    tmp_path: Path,
) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            *sum(
                (
                    [
                        {"type": "session.updated"},
                        {"type": "input_audio_buffer.committed"},
                        {"type": "response.text.delta", "delta": '{"summary":"ok"}'},
                        {"type": "response.done", "response": {"status": "completed"}},
                    ]
                    for _ in range(2)
                ),
                [],
            ),
        ]
    )
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: socket,
    )

    for sequence in (7, 8):
        result = adapter.understand_video(
            VideoUnderstandingRequest(
                video_ref="private-video-ref",
                frame_refs=[str(frame)],
                metadata={"frame_sequence": sequence},
            )
        )
        assert result.errors == []

    diagnostics = adapter.last_observation_diagnostics
    assert diagnostics == {
        "transport": "websocket",
        "session_generation": 1,
        "connection_reused": True,
        "reconnect_count": 0,
        "target_sequence": 8,
        "completed_sequence": 8,
        "first_delta_latency_ms": diagnostics["first_delta_latency_ms"],
        "total_observation_latency_ms": diagnostics["total_observation_latency_ms"],
    }
    assert isinstance(diagnostics["first_delta_latency_ms"], int)
    assert diagnostics["first_delta_latency_ms"] >= 0
    assert diagnostics["total_observation_latency_ms"] >= diagnostics["first_delta_latency_ms"]
    serialized = json.dumps(diagnostics)
    assert str(frame) not in serialized
    assert "private-video-ref" not in serialized
    assert "summary" not in serialized


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


def test_realtime_adapter_normalizes_large_jpeg_before_sending(
    monkeypatch,
    tmp_path: Path,
) -> None:
    frame = _frame(tmp_path, "large.jpg", bytes(200_000))
    socket = FakeWebSocket(_successful_responses())
    ffmpeg_calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        ffmpeg_calls.append(args)
        assert kwargs["input"] == frame.read_bytes()
        return SimpleNamespace(returncode=0, stdout=b"\xff\xd8small-jpeg\xff\xd9", stderr=b"")

    monkeypatch.setattr(qwen_realtime.subprocess, "run", run)
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: socket,
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == []
    assert ffmpeg_calls
    assert base64.b64decode(socket.sent[3]["image"]) == b"\xff\xd8small-jpeg\xff\xd9"


def test_realtime_adapter_normalization_uses_remaining_provider_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    frame = _frame(tmp_path, "large.jpg", bytes(200_000))
    socket = FakeWebSocket(_successful_responses())
    times = iter(
        [
            100.0,
            100.8,
            100.9,
            101.0,
            101.1,
            101.2,
            101.3,
            101.4,
            101.5,
            101.6,
            101.7,
            101.8,
            101.9,
            102.0,
        ]
    )
    ffmpeg_timeouts: list[float] = []

    def run(_args: list[str], **kwargs: Any) -> SimpleNamespace:
        ffmpeg_timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=b"\xff\xd8small-jpeg\xff\xd9", stderr=b"")

    monkeypatch.setattr(qwen_realtime.subprocess, "run", run)
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key", timeout_seconds=3.0),
        connect=lambda *_args, **_kwargs: socket,
        clock=lambda: next(times),
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == []
    assert len(ffmpeg_timeouts) == 1
    assert abs(ffmpeg_timeouts[0] - 2.2) < 0.001


def test_realtime_adapter_sanitizes_invalid_json_and_closes_failed_connection(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {"type": "response.text.delta", "delta": "secret-provider-garbage"},
            {"type": "response.done", "response": {"status": "completed"}},
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


def test_realtime_adapter_accepts_markdown_fenced_json_response(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {"type": "response.text.delta", "delta": "```json\n"},
            {"type": "response.text.delta", "delta": '{"summary":"杯子","objects":["杯子"]}'},
            {"type": "response.text.delta", "delta": "\n```"},
            {"type": "response.done", "response": {"status": "completed"}},
        ]
    )
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: socket,
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == []
    assert result.summary == "杯子"
    assert result.objects == ["杯子"]


def test_realtime_adapter_discards_non_object_timestamp_items(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {
                "type": "response.text.delta",
                "delta": json.dumps(
                    {
                        "summary": "杯子",
                        "objects": ["杯子"],
                        "timestamps": ["当前帧", {"start_ms": 0, "description": "当前帧"}],
                    },
                    ensure_ascii=False,
                ),
            },
            {"type": "response.done", "response": {"status": "completed"}},
        ]
    )
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=lambda *_args, **_kwargs: socket,
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == []
    assert result.timestamps == [{"start_ms": 0, "description": "当前帧"}]


def test_realtime_adapter_maps_timeout_and_disconnect_to_structured_errors(tmp_path: Path) -> None:
    frame = _frame(tmp_path)

    class TimeoutSocket(FakeWebSocket):
        def recv(self, timeout: float | None = None) -> str:
            if self.responses:
                return super().recv(timeout)
            raise TimeoutError("raw timeout details")

    timeout_socket = TimeoutSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
        ]
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
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
        ]
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


def test_reconnect_backoff_cannot_outlive_round_deadline(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    now = [10.0]
    sleeps: list[float] = []
    connect_calls = 0

    def connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        raise ConnectionError("offline")

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key", timeout_seconds=0.1),
        connect=connect,
        clock=lambda: now[0],
        sleep=sleep,
    )
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    assert adapter.understand_video(request).errors[0]["code"] == "provider_connection_failed"
    timed_out = adapter.understand_video(request)

    assert timed_out.errors[0]["code"] == "provider_timeout"
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.1
    assert connect_calls == 1


def test_realtime_adapter_counts_failed_reconnect_attempts_independently_from_sessions(
    tmp_path: Path,
) -> None:
    frame = _frame(tmp_path)
    attempts = 0
    success_socket = FakeWebSocket(_successful_responses())

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ConnectionError("offline")
        return success_socket

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"),
        connect=connect,
        sleep=lambda _seconds: None,
    )
    request = VideoUnderstandingRequest(
        video_ref="v",
        frame_refs=[str(frame)],
        metadata={"frame_sequence": 9},
    )

    first_failure = adapter.understand_video(request)
    first_diagnostics = adapter.last_observation_diagnostics
    second_failure = adapter.understand_video(request)
    second_diagnostics = adapter.last_observation_diagnostics
    success = adapter.understand_video(request)
    success_diagnostics = adapter.last_observation_diagnostics

    assert first_failure.errors[0]["code"] == "provider_connection_failed"
    assert first_diagnostics["session_generation"] is None
    assert first_diagnostics["reconnect_count"] == 0
    assert second_failure.errors[0]["code"] == "provider_connection_failed"
    assert second_diagnostics["session_generation"] is None
    assert second_diagnostics["reconnect_count"] == 1
    assert success.errors == []
    assert success_diagnostics["session_generation"] == 1
    assert success_diagnostics["reconnect_count"] == 2
    assert success_diagnostics["completed_sequence"] == 9


def test_realtime_adapter_resets_backoff_after_handshake_even_if_round_fails(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    attempts = 0
    socket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {"type": "response.done", "response": {"status": "failed"}},
        ]
    )

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("offline")
        return socket

    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key"), connect=connect, sleep=lambda _seconds: None
    )
    request = VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])

    assert adapter.understand_video(request).errors[0]["code"] == "provider_connection_failed"
    result = adapter.understand_video(request)

    assert result.errors[0]["code"] == "provider_incomplete_response"
    assert adapter.connection_failures == 0
    assert adapter.successful_observations == 0


def test_realtime_adapter_uses_one_deadline_and_remaining_recv_time(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    times = iter([10.0, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5])
    recv_timeouts: list[float] = []
    socket = FakeWebSocket(_successful_responses())
    original_recv = socket.recv

    def recv(timeout: float | None = None) -> str:
        recv_timeouts.append(timeout or 0.0)
        return original_recv(timeout)

    socket.recv = recv
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="test-key", timeout_seconds=5.0),
        connect=lambda *_args, **_kwargs: socket,
        clock=lambda: next(times),
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="v", frame_refs=[str(frame)])
    )

    assert result.errors == []
    assert recv_timeouts == sorted(recv_timeouts, reverse=True)
    assert recv_timeouts[0] > recv_timeouts[-1]


def test_realtime_adapter_rotates_after_twenty_successes_and_closes(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    sockets = [
        FakeWebSocket(
            [
                {"type": "session.created"},
                {"type": "session.updated"},
                *sum(
                    (
                        [
                            {"type": "session.updated"},
                            {"type": "input_audio_buffer.committed"},
                            {"type": "response.text.delta", "delta": '{"summary":"ok"}'},
                            {"type": "response.done", "response": {"status": "completed"}},
                        ]
                        for _ in range(20)
                    ),
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
