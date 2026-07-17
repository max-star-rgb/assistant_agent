import json

from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.vision_client import (
    MockVisionUnderstandingClient,
    VisionUnderstandingClient,
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.services.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.services.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoFrame
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.video_tool import VideoUnderstandingTool


def test_video_understanding_tool_returns_structured_result() -> None:
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(
        {"video_ref": "mock://video/demo", "user_query": "视频里有什么"}
    )

    assert result.success is True
    assert result.tool_name == "video_understanding"
    assert result.output_ref == "mock://video/understanding/demo"
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert result.data["summary"]


def test_vision_understanding_main_tool_handles_explicit_video_inputs() -> None:
    result = VisionUnderstandingTool(client=MockVisionUnderstandingClient()).run(
        {
            "video_ref": "mock://video/demo",
            "frame_refs": ["/tmp/private-frame.jpg"],
            "user_query": "视频里有什么",
            "memory_context": "上一帧看到一只杯子",
        }
    )

    observation = observation_from_tool_result(result)

    assert result.success is True
    assert result.tool_name == "vision_understanding"
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert result.data["source"] == "recent_frame_fallback"
    assert result.contract is not None
    assert result.contract.capability == "video_understanding"
    assert observation.structured_output is not None
    assert observation.structured_output["summary"]
    assert "provider" not in observation.structured_output
    assert "model" not in observation.structured_output
    assert "latency_ms" not in observation.structured_output
    assert "private-frame" not in json.dumps(observation.structured_output, ensure_ascii=False)


def test_video_understanding_alias_and_main_tool_share_unified_client() -> None:
    class RecordingVisionClient:
        def __init__(self) -> None:
            self.requests: list[VisionUnderstandingRequest] = []

        def understand(self, request: VisionUnderstandingRequest) -> VisionUnderstandingResult:
            self.requests.append(request)
            return VisionUnderstandingResult(
                summary=f"统一视觉结果：{request.video_ref}",
                objects=["杯子"],
                provider="fake_realtime",
                model="fake-realtime-vision",
                output_ref=f"fake://vision/{len(self.requests)}",
                latency_ms=2,
            )

    client: VisionUnderstandingClient = RecordingVisionClient()

    main = VisionUnderstandingTool(client=client)
    alias = VideoUnderstandingTool(client=client)

    main_result = main.run({"video_ref": "mock://video/main", "user_query": "看一下"})
    alias_result = alias.run({"video_ref": "mock://video/alias", "user_query": "看一下"})

    assert [request.video_ref for request in client.requests] == [
        "mock://video/main",
        "mock://video/alias",
    ]
    assert main_result.tool_name == "vision_understanding"
    assert alias_result.tool_name == "video_understanding"
    assert main_result.data is not None
    assert alias_result.data is not None
    assert main_result.data["provider"] == "fake_realtime"
    assert alias_result.data["provider"] == "fake_realtime"


def test_video_understanding_tool_accepts_request_model() -> None:
    request = VideoUnderstandingRequest(video_ref="mock://video/product-clip", user_query="识别商品")

    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(request)

    assert result.success is True
    assert result.output_ref == "mock://video/understanding/product-clip"


def test_video_understanding_tool_returns_structured_missing_video_error() -> None:
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run({"user_query": "总结视频"})

    assert result.success is False
    assert result.tool_name == "video_understanding"
    assert result.error == "video_missing_input: VideoUnderstandingRequest requires video_ref."
    assert result.contract is not None
    assert result.contract.capability == "video_understanding"
    assert result.contract.status == "failed"
    assert result.contract.errors[0].code == "video_missing_input"


def test_default_registry_contains_video_understanding_tool_with_mock_adapter() -> None:
    tool = create_default_registry().get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, MockVideoUnderstandingAdapter)


def test_video_understanding_tool_spec_carries_realtime_camera_call_policy() -> None:
    spec = create_default_registry().get_spec("video_understanding")
    rendered = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)

    assert "当前实时镜头" in rendered
    assert "显式视频引用" in rendered
    assert "当前画面" in rendered
    assert "视觉事实" in rendered
    assert "不要传内部帧路径" in rendered
    assert "当前 turn 的视频引用" in rendered
    assert "证据不足" in rendered
    assert "video_ref or video_ids" in rendered


def test_video_understanding_tool_does_not_call_http_or_sdk() -> None:
    class LocalOnlyAdapter:
        def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
            return VideoUnderstandingResult(
                summary=f"local only: {request.video_ref}",
                provider="mock",
                output_ref="mock://video/understanding/local-only",
            )

    result = VideoUnderstandingTool(adapter=LocalOnlyAdapter()).run({"video_ref": "mock://video/local"})

    assert result.success is True
    assert result.output_ref == "mock://video/understanding/local-only"


def test_video_understanding_tool_traces_rolling_snapshot_diagnostics() -> None:
    memory = RealtimeVideoMemoryStore()
    frame = SemanticKeyframeRecord(
        frame_id="frame-7",
        uri="/tmp/private-frame.jpg",
        sequence=7,
        timestamp_ms=7000,
    )
    memory.record_success(
        "video-a",
        frame,
        VideoUnderstandingResult(
            summary="桌上有杯子",
            objects=["杯子"],
            provider="qwen",
            model="qwen-vl-max",
            output_ref="provider://video/rolling/7",
        ),
        diagnostics=RealtimeVideoObservationDiagnostics(
            observation_latency_ms=83,
            published_at_ms=10_000,
        ),
    )
    memory.mark_pending("video-a", pending_count=1, in_flight=True)
    tool = VideoUnderstandingTool(
        adapter=MockVideoUnderstandingAdapter(),
        memory_store=memory,
        wall_clock_ms=lambda: 10_145,
    )

    result = tool.run({"video_ref": "video-a", "user_query": "眼前有什么？"})

    assert result.trace_summary == {
        "source": "rolling_video_memory",
        "snapshot_age_ms": 145,
        "observation_latency_ms": 83,
        "pending_count": 1,
        "in_flight": True,
        "fallback_used": False,
        "snapshot_sequence": 7,
        "provider": "qwen",
        "model": "qwen-vl-max",
    }
    assert "private-frame" not in str(result.trace_summary)


def test_video_understanding_tool_marks_query_time_recent_frame_fallback() -> None:
    memory = RealtimeVideoMemoryStore()
    frame = SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/private-frame.jpg", sequence=1, timestamp_ms=1000
    )
    memory.record_success(
        "video-a",
        frame,
        VideoUnderstandingResult(
            summary="旧状态",
            provider="qwen",
            model="old-model",
            output_ref="provider://video/rolling/1",
        ),
        diagnostics=RealtimeVideoObservationDiagnostics(
            observation_latency_ms=80,
            published_at_ms=10_000,
        ),
    )
    memory.record_failure(
        "video-a",
        frame,
        {"code": "provider_timeout", "message": "timed out", "recoverable": True},
    )
    context = InMemoryVideoContextStore()
    context.append_frame(
        VideoFrame(video_id="video-a", frame_id="raw-1", uri="frame.jpg", sequence=1)
    )
    tool = VideoUnderstandingTool(
        adapter=MockVideoUnderstandingAdapter(),
        context_store=context,
        memory_store=memory,
        wall_clock_ms=lambda: 10_200,
    )

    result = tool.run({"video_ref": "video-a"})

    assert result.success is True
    assert result.trace_summary is not None
    assert result.trace_summary["source"] == "recent_frame_fallback"
    assert result.trace_summary["fallback_used"] is True
    assert result.trace_summary["snapshot_age_ms"] == 200
    assert result.trace_summary["provider"] == "mock"
    assert result.trace_summary["model"] == "mock-video-understanding"
