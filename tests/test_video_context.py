from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import run_assistant_request
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore, SemanticKeyframeRecord
from assistant_agent.services.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoFrame, load_demo_video_frames
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.video_tool import VideoUnderstandingTool


class CountingVideoAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        self.calls += 1
        return VideoUnderstandingResult(
            summary="最近帧回退结果",
            objects=["回退物体"],
            provider="counting-video",
            model="counting-test",
            output_ref="provider://video/counting/result",
        )


def _semantic_result(*, summary: str, objects: list[str]) -> VideoUnderstandingResult:
    return VideoUnderstandingResult(
        summary=summary,
        objects=objects,
        provider="background-test",
        model="background-model",
        output_ref="provider://video/background/result",
    )


def _semantic_keyframe() -> SemanticKeyframeRecord:
    return SemanticKeyframeRecord(
        frame_id="frame-1",
        uri="/tmp/frame-1.jpg",
        sequence=1,
        timestamp_ms=1000,
    )


def test_video_context_store_keeps_recent_three_frame_window() -> None:
    store = InMemoryVideoContextStore(window_size=3)

    for index in range(1, 6):
        store.append_frame(
            VideoFrame(
                video_id="video1",
                frame_id=f"frame_{index}",
                uri=f"frame_{index}.jpg",
                sequence=index,
            )
        )

    frames = store.get_recent_frames("video1")

    assert [frame.frame_id for frame in frames] == ["frame_3", "frame_4", "frame_5"]
    assert [frame.uri for frame in frames] == ["frame_3.jpg", "frame_4.jpg", "frame_5.jpg"]


def test_video_context_store_remove_video_returns_and_clears_frames() -> None:
    store = InMemoryVideoContextStore(window_size=3)
    frame = VideoFrame(
        video_id="video1",
        frame_id="frame_1",
        uri="frame_1.jpg",
        sequence=1,
    )
    store.append_frame(frame)

    removed = store.remove_video("video1")

    assert removed == [frame]
    assert store.get_recent_frames("video1") == []
    assert store.remove_video("video1") == []


def test_demo_video1_frames_load_into_recent_three_frame_context() -> None:
    store = InMemoryVideoContextStore(window_size=3)

    loaded = load_demo_video_frames(store, "video1")
    frames = store.get_recent_frames("video1")

    assert len(loaded) == 5
    assert [frame.frame_id for frame in frames] == ["frame_000003", "frame_000004", "frame_000005"]
    assert all(frame.uri.endswith(".jpg") for frame in frames)


def test_video_understanding_tool_adds_recent_context_frame_snapshot() -> None:
    store = InMemoryVideoContextStore(window_size=3)
    for index in range(1, 5):
        store.append_frame(
            VideoFrame(
                video_id="video1",
                frame_id=f"frame_{index}",
                uri=f"/tmp/frame_{index}.jpg",
                sequence=index,
            )
        )

    captured = {}

    class CapturingAdapter:
        def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
            captured["request"] = request
            return MockVideoUnderstandingAdapter().understand_video(request)

    result = VideoUnderstandingTool(adapter=CapturingAdapter(), context_store=store).run(
        {"video_ids": ["video1"], "user_query": "视频里发生了什么"}
    )

    request = captured["request"]
    assert result.success is True
    assert request.video_ref == "video1"
    assert request.context_id == "video1"
    assert request.frame_refs == ["/tmp/frame_2.jpg", "/tmp/frame_3.jpg", "/tmp/frame_4.jpg"]
    assert request.metadata["context_window_size"] == 3
    assert request.metadata["context_frame_count"] == 3
    assert request.metadata["context_frame_ids"] == ["frame_2", "frame_3", "frame_4"]


def test_video_tool_uses_healthy_memory_without_provider_call() -> None:
    adapter = CountingVideoAdapter()
    memory = RealtimeVideoMemoryStore()
    memory.record_success(
        "video-a",
        _semantic_keyframe(),
        _semantic_result(summary="桌上有杯子", objects=["杯子"]),
    )
    tool = VideoUnderstandingTool(
        adapter=adapter,
        context_store=InMemoryVideoContextStore(),
        memory_store=memory,
    )

    result = tool.run({"video_ref": "video-a", "user_query": "眼前有什么？"})

    assert result.success is True
    assert result.data["source"] == "rolling_video_memory"
    assert result.data["objects"] == ["杯子"]
    assert result.data["snapshot_sequence"] == 1
    assert adapter.calls == 0


def test_video_tool_falls_back_after_latest_observation_failure() -> None:
    adapter = CountingVideoAdapter()
    memory = RealtimeVideoMemoryStore()
    frame = _semantic_keyframe()
    memory.record_success(
        "video-a",
        frame,
        _semantic_result(summary="旧状态", objects=["旧物体"]),
    )
    memory.record_failure(
        "video-a",
        frame,
        {"code": "provider_timeout", "message": "timed out", "recoverable": True},
    )
    context = InMemoryVideoContextStore()
    context.append_frame(
        VideoFrame(video_id="video-a", frame_id="raw-1", uri="/tmp/raw-1.jpg", sequence=1)
    )
    tool = VideoUnderstandingTool(adapter=adapter, context_store=context, memory_store=memory)

    result = tool.run({"video_ref": "video-a"})

    assert result.success is True
    assert result.data["source"] == "recent_frame_fallback"
    assert adapter.calls == 1


def test_observation_context_forces_provider_even_with_healthy_memory() -> None:
    adapter = CountingVideoAdapter()
    memory = RealtimeVideoMemoryStore()
    memory.record_success(
        "video-a",
        _semantic_keyframe(),
        _semantic_result(summary="已有状态", objects=["已有物体"]),
    )
    tool = VideoUnderstandingTool(adapter=adapter, memory_store=memory)

    result = tool.run(
        {"video_ref": "video-a", "frame_refs": ["/tmp/keyframe.jpg"]},
        ToolContext(metadata={"realtime_video_observation": True}),
    )

    assert result.success is True
    assert result.data["source"] == "background_keyframe_observation"
    assert adapter.calls == 1


def test_agent_video_understanding_uses_demo_video_context_snapshot() -> None:
    store = InMemoryVideoContextStore(window_size=3)
    registry = create_default_registry(video_context_store=store)
    runtime = AgentGraphRuntime(registry=registry, video_context_store=store)

    artifacts = run_assistant_request(
        UserRequest(user_id="u1", session_id="s1", text="总结这个视频", video_ids=["video1"]),
        runtime=runtime,
        load_env=False,
    )

    video_result = next(result for result in artifacts.state.tool_results if result.tool_name == "video_understanding")
    timestamps = video_result.data["timestamps"]
    assert artifacts.state.status == "completed"
    assert video_result.success is True
    assert video_result.data["summary"]
    assert [item["frame_ref"].rsplit("/", 1)[-1] for item in timestamps] == [
        "frame_000003.jpg",
        "frame_000004.jpg",
        "frame_000005.jpg",
    ]
