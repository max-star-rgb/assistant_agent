from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.assistant_run_service import run_assistant_request
from multimodal_agent.services.video_adapter import MockVideoUnderstandingAdapter
from multimodal_agent.services.video_context import InMemoryVideoContextStore, VideoFrame, load_demo_video_frames
from multimodal_agent.tools.registry import create_default_registry
from multimodal_agent.tools.video_tool import VideoUnderstandingTool


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
