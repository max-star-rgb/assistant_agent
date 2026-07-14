from __future__ import annotations

import asyncio
import importlib
import threading
from dataclasses import replace
from pathlib import Path

from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.services.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.services.video_context import VideoFrame
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.video_tool import VideoUnderstandingTool
from assistant_agent.video_ai.keyframe.collector import KeyframeCollectionResult
from assistant_agent.video_ai.types import (
    FrameProcessingResult,
    KeyframeChangeMetrics,
    VideoFrame as AIVideoFrame,
)


VIDEO_ID = "agent-service-video-observer-test"


class AlwaysSelectCollector:
    def collect(self, frame: AIVideoFrame) -> KeyframeCollectionResult:
        processing = FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=frame.timestamp_seconds,
            sampled=True,
            sampling_rate=5.0,
            metrics=KeyframeChangeMetrics(keyframe_score=1.0),
            keyframe_selected=True,
            qwen_called=False,
            latency_ms=1,
            decision_reason="test",
        )
        return KeyframeCollectionResult(processing=processing, selected_frame=frame)


class RecordingVideoTool(VideoUnderstandingTool):
    def __init__(self) -> None:
        super().__init__(adapter=MockVideoUnderstandingAdapter())
        self.context_metadata: dict[str, object] = {}
        self.inputs: list[dict[str, object]] = []

    def _run(self, input: VideoUnderstandingRequest, context: ToolContext) -> ToolResult:
        self.context_metadata = dict(context.metadata)
        self.inputs.append(input.model_dump(mode="python"))
        return super()._run(input, context)


class BlockingVideoAdapter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.sequences: list[int] = []

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        sequence = int(Path(request.frame_refs[-1]).stem.rsplit("-", 1)[-1])
        self.sequences.append(sequence)
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("test release timed out")
        return VideoUnderstandingResult(
            summary=f"frame {sequence}",
            objects=[f"object-{sequence}"],
            provider="blocking-test",
            output_ref=f"provider://video/test/{sequence}",
        )


class FailingVideoAdapter:
    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        return VideoUnderstandingResult(
            summary="observation failed",
            provider="failing-test",
            output_ref=f"provider://video/test/{request.video_ref}",
            errors=[
                {
                    "code": "provider_bad_response",
                    "message": "bad response from /home/user/private/provider/path",
                    "recoverable": False,
                }
            ],
        )


def _decoded_frame(root: Path, *, sequence: int) -> VideoFrame:
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    path = raw_root / f"raw-{sequence}.jpg"
    path.write_bytes(b"\xff\xd8jpeg\xff\xd9")
    return VideoFrame(
        video_id=VIDEO_ID,
        frame_id=f"frame-{sequence:06d}",
        uri=str(path),
        sequence=sequence,
        timestamp_ms=sequence * 1000,
        fingerprint=tuple([min(255, sequence * 40)] * 16),
        fingerprint_width=4,
        fingerprint_height=4,
    )


def test_observer_validates_and_executes_selected_frame_through_tool_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        tool = RecordingVideoTool()
        registry = ToolRegistry()
        registry.register(tool)
        memory = RealtimeVideoMemoryStore()
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
        )

        result = await observer.submit(_decoded_frame(tmp_path, sequence=1))
        await observer.wait_idle()

        assert result.keyframe_selected is True
        assert tool.context_metadata["realtime_video_observation"] is True
        assert tool.inputs[0]["video_ref"] == VIDEO_ID
        assert memory.snapshot(VIDEO_ID).healthy is True
        await observer.close()

    asyncio.run(scenario())


def test_observer_records_deterministic_phase_timings(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()))
        memory = RealtimeVideoMemoryStore()
        times = iter(
            [
                1_000_000_000,
                1_002_000_000,
                1_009_000_000,
                1_010_000_000,
                1_090_000_000,
            ]
        )
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
            clock_ns=lambda: next(times),
            wall_clock_ms=lambda: 10_000,
        )
        frame = replace(
            _decoded_frame(tmp_path, sequence=1),
            metadata={"h264_decode_latency_ms": 4},
        )

        await observer.submit(frame)
        await observer.wait_idle()

        snapshot = memory.snapshot(VIDEO_ID)
        assert snapshot is not None
        diagnostics = snapshot.observation_diagnostics
        assert diagnostics is not None
        assert diagnostics.h264_decode_latency_ms == 4
        assert diagnostics.keyframe_selection_latency_ms == 2
        assert diagnostics.queue_wait_latency_ms == 7
        assert diagnostics.observation_latency_ms == 80
        assert diagnostics.published_at_ms == 10_000
        await observer.close()

    asyncio.run(scenario())


def test_observer_keeps_one_inflight_and_latest_pending_frame(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = BlockingVideoAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        memory = RealtimeVideoMemoryStore()
        keyframe_root = tmp_path / "keyframes"
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=keyframe_root,
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(adapter.started.wait, 2.0)
        await observer.submit(_decoded_frame(tmp_path, sequence=2))
        await observer.submit(_decoded_frame(tmp_path, sequence=3))

        retained_names = sorted(path.name for path in keyframe_root.rglob("*.jpg"))
        assert "frame-000002.jpg" not in retained_names
        assert "frame-000003.jpg" in retained_names

        adapter.release.set()
        await observer.wait_idle()

        assert adapter.sequences == [1, 3]
        snapshot = memory.snapshot(VIDEO_ID)
        assert snapshot is not None
        assert [frame.sequence for frame in snapshot.keyframes] == [1, 3]
        await observer.close()
        assert memory.snapshot(VIDEO_ID) is None
        assert list(keyframe_root.rglob("*.jpg")) == []

    asyncio.run(scenario())


def test_failed_observation_records_failure_and_deletes_selected_artifact(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=FailingVideoAdapter()))
        memory = RealtimeVideoMemoryStore()
        keyframe_root = tmp_path / "keyframes"
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=keyframe_root,
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        await observer.wait_idle()

        snapshot = memory.snapshot(VIDEO_ID)
        assert snapshot is not None
        assert snapshot.healthy is False
        assert snapshot.last_error["code"] == "provider_bad_response"
        assert "private" not in snapshot.last_error["message"]
        assert list(keyframe_root.rglob("*.jpg")) == []
        await observer.close()

    asyncio.run(scenario())
