"""Offline regressions for static-media and live-view visual boundaries."""

from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    SemanticKeyframeRecord,
)
from assistant_agent.media.vision import vision_client as vision_client_module
from assistant_agent.media.vision.models import (
    VideoUnderstandingResult,
    VisionUnderstandingRequest,
    VisualUnderstandingResult,
)
from assistant_agent.media.vision.vision_client import (
    AdapterVisionUnderstandingClient,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    MediaInspectTool,
)


class _ImageAdapter:
    provider = "fake-vision"
    model = "fake-image-model"

    def understand(self, _input: object) -> VisualUnderstandingResult:
        return VisualUnderstandingResult(summary="静态图片事实")


class _VideoAdapter:
    provider = "fake-video"
    model = "fake-video-model"

    def __init__(self) -> None:
        self.calls = 0

    def understand_video(self, _request: object) -> VideoUnderstandingResult:
        self.calls += 1
        return VideoUnderstandingResult(
            summary="显式视频 Provider 事实",
            provider=self.provider,
            model=self.model,
            output_ref="provider://video/explicit",
            latency_ms=7,
        )


def test_image_result_reports_real_elapsed_time_and_media_provenance(
    monkeypatch,
) -> None:
    timestamps = iter((10.0, 10.125))
    monkeypatch.setattr(
        vision_client_module,
        "perf_counter",
        lambda: next(timestamps),
    )
    client = AdapterVisionUnderstandingClient(image_adapter=_ImageAdapter())

    result = client.understand(
        VisionUnderstandingRequest(
            image_ids=["https://example.com/image.jpg"],
            question="图片内容是什么？",
        )
    )

    assert result.latency_ms == 125
    assert result.source == "request_image"
    assert result.media_kind == "image"
    assert result.media_refs == ["https://example.com/image.jpg"]


def test_explicit_video_does_not_consume_live_snapshot() -> None:
    video_id = "video-1"
    memory_store = _memory_store_with_snapshot(video_id)
    video_adapter = _VideoAdapter()
    client = AdapterVisionUnderstandingClient(video_adapter=video_adapter)
    tool = MediaInspectTool(client=client, memory_store=memory_store)

    result = tool.run(
        VisionUnderstandingRequest(
            video_ids=[video_id],
            question="分析这个视频",
        ),
        ToolContext(metadata={"request_metadata": {}}),
    )

    assert result.success is True
    assert video_adapter.calls == 1
    assert result.data is not None
    assert result.data["summary"] == "显式视频 Provider 事实"
    assert result.data["source"] == "explicit_video"
    assert result.data["media_kind"] == "explicit_video"
    assert result.data["media_refs"] == [video_id]


def test_live_view_reads_governed_snapshot_without_calling_video_provider() -> None:
    video_id = "agent-service-video-1"
    memory_store = _memory_store_with_snapshot(video_id)
    video_adapter = _VideoAdapter()
    client = AdapterVisionUnderstandingClient(video_adapter=video_adapter)
    tool = LiveViewInspectTool(client=client, memory_store=memory_store)

    result = tool.run(
        VisionUnderstandingRequest(
            video_ids=[video_id],
            question="当前画面是什么？",
        ),
        ToolContext(
            metadata={
                "request_metadata": {
                    "transport": "agent_service_websocket",
                    "gateway": {
                        "session_config": {
                            "entry_profile": "agent_service",
                        }
                    },
                }
            }
        ),
    )

    assert result.success is True
    assert video_adapter.calls == 0
    assert result.data is not None
    assert result.data["summary"] == "实时滚动画面事实"
    assert result.data["source"] == "rolling_video_memory"
    assert result.data["media_kind"] == "live_view"
    assert result.data["media_refs"] == [video_id]


def _memory_store_with_snapshot(video_id: str) -> RealtimeVideoMemoryStore:
    store = RealtimeVideoMemoryStore()
    store.record_success(
        video_id,
        SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=1_000,
        ),
        VideoUnderstandingResult(
            summary="实时滚动画面事实",
            provider="fake-realtime",
            model="fake-realtime-model",
            output_ref="provider://video/live",
            latency_ms=3,
        ),
    )
    return store
