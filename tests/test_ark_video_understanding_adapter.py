import json
import sys
import types

from multimodal_agent.providers.ark_video_understanding import (
    ArkVideoUnderstandingAdapter,
    ArkVideoUnderstandingConfig,
    build_ark_video_input,
)
from multimodal_agent.schemas.perception import VideoUnderstandingRequest


def test_build_ark_video_input_uses_recent_frame_refs(tmp_path) -> None:
    frame_1 = tmp_path / "frame_1.jpg"
    frame_2 = tmp_path / "frame_2.jpg"
    frame_1.write_bytes(b"fake")
    frame_2.write_bytes(b"fake")

    payload = build_ark_video_input(
        VideoUnderstandingRequest(
            video_ref="video1",
            frame_refs=[str(frame_1), str(frame_2)],
            user_query="总结这个视频",
        )
    )

    content = payload[0]["content"]
    assert content[0] == {"type": "input_image", "image_url": f"file://{frame_1.resolve()}"}
    assert content[1] == {"type": "input_image", "image_url": f"file://{frame_2.resolve()}"}
    assert content[2]["type"] == "input_text"
    assert "总结这个视频" in content[2]["text"]
    assert "2 张" in content[2]["text"]


def test_ark_video_adapter_calls_sdk_with_multiple_frames(monkeypatch, tmp_path) -> None:
    frame_1 = tmp_path / "frame_1.jpg"
    frame_2 = tmp_path / "frame_2.jpg"
    frame_1.write_bytes(b"fake")
    frame_2.write_bytes(b"fake")
    captured = {}

    class FakeResponses:
        async def create(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "summary": "视频里有人展示白色运动鞋。",
                        "objects": ["白色运动鞋"],
                        "actions": ["展示商品"],
                        "events": ["鞋子旋转"],
                        "scene": "室内展示台",
                        "products": ["白色运动鞋"],
                        "brands": [],
                        "colors": ["白色"],
                        "materials": ["皮革", "橡胶"],
                        "text_in_video": [],
                        "timestamps": [{"start_ms": 0, "end_ms": 1000, "description": "展示鞋面"}],
                        "style_tags": ["商品展示"],
                        "confidence": 0.87,
                    },
                    ensure_ascii=False,
                )
            )

    class FakeAsyncArk:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", types.SimpleNamespace(AsyncArk=FakeAsyncArk))

    adapter = ArkVideoUnderstandingAdapter(
        ArkVideoUnderstandingConfig(
            api_key="ark-video-key",
            base_url="https://ark.local/api/v3",
            model="ark-video-model",
        )
    )
    result = adapter.understand_video(
        VideoUnderstandingRequest(
            video_ref="video1",
            frame_refs=[str(frame_1), str(frame_2)],
            user_query="总结这个视频",
        )
    )

    assert captured["base_url"] == "https://ark.local/api/v3"
    assert captured["api_key"] == "ark-video-key"
    assert captured["model"] == "ark-video-model"
    assert captured["input"][0]["content"][0]["image_url"] == f"file://{frame_1.resolve()}"
    assert result.provider == "ark"
    assert result.model == "ark-video-model"
    assert result.output_ref == "provider://video/ark/video1"
    assert result.summary == "视频里有人展示白色运动鞋。"
    assert result.objects == ["白色运动鞋"]
    assert result.actions == ["展示商品"]
    assert result.errors == []


def test_ark_video_adapter_ignores_invalid_socks_proxy_env(monkeypatch, tmp_path) -> None:
    frame = tmp_path / "frame_1.jpg"
    frame.write_bytes(b"fake")
    captured = {}

    class FakeResponses:
        async def create(self, *, model, input):
            captured["all_proxy_during_call"] = __import__("os").environ.get("ALL_PROXY")
            return types.SimpleNamespace(output_text='{"summary":"ok"}')

    class FakeAsyncArk:
        def __init__(self, *, base_url, api_key):
            captured["all_proxy_during_init"] = __import__("os").environ.get("ALL_PROXY")
            self.responses = FakeResponses()

    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:17891/")
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", types.SimpleNamespace(AsyncArk=FakeAsyncArk))

    adapter = ArkVideoUnderstandingAdapter(ArkVideoUnderstandingConfig(api_key="ark-video-key"))
    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="video1", frame_refs=[str(frame)]))

    assert result.errors == []
    assert captured["all_proxy_during_init"] is None
    assert captured["all_proxy_during_call"] is None
    assert __import__("os").environ["ALL_PROXY"] == "socks://127.0.0.1:17891/"


def test_ark_video_adapter_error_includes_redacted_diagnostics(monkeypatch, tmp_path) -> None:
    frame = tmp_path / "frame_1.jpg"
    frame.write_bytes(b"fake")

    class FakeArkNotFoundError(Exception):
        request_id = "request_123"
        status_code = 404
        code = "ModelNotOpen"

    class FakeResponses:
        async def create(self, *, model, input):
            raise FakeArkNotFoundError("Error code: 404, request_id: request_123")

    class FakeAsyncArk:
        def __init__(self, *, base_url, api_key):
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", types.SimpleNamespace(AsyncArk=FakeAsyncArk))

    adapter = ArkVideoUnderstandingAdapter(
        ArkVideoUnderstandingConfig(
            api_key="ark-video-key",
            base_url="https://ark.local/api/v3",
            model="ark-video-model",
        )
    )
    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="video1", frame_refs=[str(frame)]))

    assert result.errors[0]["code"] == "provider_bad_response"
    message = result.errors[0]["message"]
    assert "model=ark-video-model" in message
    assert "base_url=https://ark.local/api/v3" in message
    assert "request_id=request_123" in message
    assert "ark-video-key" not in message


def test_ark_video_adapter_missing_key_returns_structured_error() -> None:
    adapter = ArkVideoUnderstandingAdapter(ArkVideoUnderstandingConfig(api_key=None))

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="video1", frame_refs=["mock://frame/1"]))

    assert result.provider == "ark"
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "ARK_VISION_API_KEY" in result.errors[0]["message"]


def test_ark_video_adapter_requires_context_frames() -> None:
    adapter = ArkVideoUnderstandingAdapter(ArkVideoUnderstandingConfig(api_key="ark-video-key"))

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="video1"))

    assert result.errors[0]["code"] == "video_missing_frames"
