import os
from pathlib import Path

import openai

from assistant_agent.providers.qwen_video_understanding import QwenVideoUnderstandingAdapter
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.video_ai.memory.state_manager import KeyframeMemoryRecord
from assistant_agent.video_ai.qwen.vision_client import QwenVLClient, QwenVLConfig, VisionObservation
from assistant_agent.video_ai.types import VideoFrame


class RecordingQwenClient:
    def __init__(self, observation: VisionObservation | None = None) -> None:
        self.observation = observation or VisionObservation(
            scene="测试桌面",
            objects=["红色方块"],
            people=[],
            actions=["静止展示"],
            changes_from_previous="颜色从蓝色变为红色",
            important_events=["出现红色方块"],
            summary="桌面上有一个红色方块。",
            provider="qwen",
            model="qwen-vl-test",
            latency_ms=23,
        )
        self.current: VideoFrame | None = None
        self.history: list[KeyframeMemoryRecord] = []
        self.previous_state_summary = ""

    def understand_keyframe(
        self,
        current_frame: VideoFrame,
        history_keyframes: list[KeyframeMemoryRecord],
        previous_state_summary: str,
    ) -> VisionObservation:
        self.current = current_frame
        self.history = list(history_keyframes)
        self.previous_state_summary = previous_state_summary
        return self.observation


def _config(*, api_key: str | None = "test-key") -> QwenVLConfig:
    return QwenVLConfig(
        api_key=api_key,
        base_url="https://qwen.local/v1",
        model="qwen-vl-test",
        timeout_seconds=7.5,
    )


def test_qwen_video_adapter_maps_ordered_frames_and_structured_result(tmp_path: Path) -> None:
    frame_1 = str(tmp_path / "frame-1.jpg")
    frame_2 = str(tmp_path / "frame-2.jpg")
    client = RecordingQwenClient()
    adapter = QwenVideoUnderstandingAdapter(_config(), client=client)

    result = adapter.understand_video(
        VideoUnderstandingRequest(
            video_ref="video-1",
            frame_refs=[frame_1, frame_2],
            user_query="识别眼前物体",
            metadata={"frame_timestamp_ms": 2500},
        )
    )

    assert [record.uri for record in client.history] == [frame_1]
    assert client.current is not None
    assert client.current.uri == frame_2
    assert client.current.timestamp_seconds == 2.5
    assert result.provider == "qwen"
    assert result.model == "qwen-vl-test"
    assert result.summary == "桌面上有一个红色方块。"
    assert result.objects == ["红色方块"]
    assert result.actions == ["静止展示"]
    assert result.events == ["出现红色方块"]
    assert result.output_ref == "provider://video/qwen/video-1"
    assert result.errors == []
    assert result.latency_ms == 23


def test_qwen_video_adapter_requires_context_frames() -> None:
    adapter = QwenVideoUnderstandingAdapter(_config(), client=RecordingQwenClient())

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="video-1"))

    assert result.provider == "qwen"
    assert result.errors[0]["code"] == "video_missing_frames"


def test_qwen_video_adapter_preserves_structured_client_failure() -> None:
    client = RecordingQwenClient(
        VisionObservation(
            summary="Qwen request failed.",
            provider="qwen",
            model="qwen-vl-test",
            errors=[
                {
                    "code": "provider_bad_response",
                    "message": "Qwen request failed.",
                    "recoverable": True,
                }
            ],
            latency_ms=17,
        )
    )
    adapter = QwenVideoUnderstandingAdapter(_config(), client=client)

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="video-1", frame_refs=["/tmp/frame-1.jpg"])
    )

    assert result.provider == "qwen"
    assert result.model == "qwen-vl-test"
    assert result.errors == [
        {
            "code": "provider_bad_response",
            "message": "Qwen request failed.",
            "recoverable": True,
        }
    ]
    assert result.latency_ms == 17


def test_qwen_client_hides_unsupported_socks_proxy_during_client_init(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    class FakeCompletions:
        def create(self, **_payload):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self) -> None:
            self.chat = type("FakeChat", (), {"completions": FakeCompletions()})()

    def fake_openai(**_kwargs):
        captured["all_proxy"] = os.environ.get("ALL_PROXY")
        return FakeClient()

    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:17891/")
    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    client = QwenVLClient(_config())

    client._chat([{"type": "text", "text": "test"}], json_response=False)

    assert captured["all_proxy"] is None
    assert os.environ["ALL_PROXY"] == "socks://127.0.0.1:17891/"
