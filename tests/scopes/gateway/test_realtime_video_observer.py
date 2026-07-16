from __future__ import annotations

import asyncio
import importlib
import threading
from dataclasses import replace
from pathlib import Path

import pytest

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


class SequenceBlockingVideoAdapter:
    def __init__(self, sequences: tuple[int, ...]) -> None:
        self.started = {sequence: threading.Event() for sequence in sequences}
        self.release = {sequence: threading.Event() for sequence in sequences}
        self.completed = {sequence: threading.Event() for sequence in sequences}
        self.sequences: list[int] = []

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        sequence = int(Path(request.frame_refs[-1]).stem.rsplit("-", 1)[-1])
        self.sequences.append(sequence)
        self.started[sequence].set()
        if not self.release[sequence].wait(timeout=5.0):
            raise TimeoutError(f"test release timed out for sequence {sequence}")
        self.completed[sequence].set()
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


class SummaryRecordingAdapter:
    def __init__(self) -> None:
        self.requests: list[VideoUnderstandingRequest] = []

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        self.requests.append(request)
        return VideoUnderstandingResult(
            summary="s" * 5_000 if len(self.requests) == 1 else "second",
            provider="summary-test",
            output_ref=f"provider://video/test/{len(self.requests)}",
        )


class CloseRecordingAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        return VideoUnderstandingResult(
            summary="ok",
            provider="close-test",
            output_ref=f"provider://video/test/{request.video_ref}",
        )

    def close(self) -> None:
        self.close_calls += 1


class BlockingCloseAdapter(CloseRecordingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.release_close = threading.Event()

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if not self.release_close.wait(timeout=5.0):
            raise TimeoutError("test close release timed out")


class SuccessThenBlockingFailureAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.failure_started = threading.Event()
        self.release_failure = threading.Event()
        self.last_observation_diagnostics: dict[str, object] = {}

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        self.calls += 1
        sequence = int(request.metadata["frame_sequence"])
        if self.calls == 1:
            self.last_observation_diagnostics = {
                "transport": "websocket",
                "session_generation": 1,
                "connection_reused": False,
                "reconnect_count": 0,
                "target_sequence": sequence,
                "completed_sequence": sequence,
                "first_delta_latency_ms": 12,
                "total_observation_latency_ms": 20,
            }
            return VideoUnderstandingResult(
                summary="first successful scene",
                objects=["cup"],
                provider="qwen",
                output_ref="provider://video/test/1",
            )
        self.last_observation_diagnostics = {
            "transport": "websocket",
            "session_generation": 2,
            "connection_reused": False,
            "reconnect_count": 1,
            "target_sequence": sequence,
            "completed_sequence": None,
            "first_delta_latency_ms": None,
            "total_observation_latency_ms": 30,
        }
        self.failure_started.set()
        if not self.release_failure.wait(timeout=5.0):
            raise TimeoutError("test release timed out")
        return VideoUnderstandingResult(
            summary="observation failed",
            provider="qwen",
            output_ref="provider://video/test/error",
            errors=[
                {
                    "code": "provider_timeout",
                    "message": "safe timeout",
                    "recoverable": True,
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


def test_default_observer_collector_selects_initial_change_and_two_second_static_frame(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_observer")
    observer = module.RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(),
        keyframe_root=tmp_path / "keyframes",
    )
    initial = replace(
        _decoded_frame(tmp_path, sequence=1),
        timestamp_ms=0,
        fingerprint=tuple([0] * 16),
    )
    changed = replace(
        _decoded_frame(tmp_path, sequence=2),
        timestamp_ms=50,
        fingerprint=tuple([255] * 16),
    )
    static_due = replace(
        _decoded_frame(tmp_path, sequence=3),
        timestamp_ms=2_100,
        fingerprint=tuple([255] * 16),
    )

    first = observer.collector.collect(module._to_ai_frame(initial))
    change = observer.collector.collect(module._to_ai_frame(changed))
    due = observer.collector.collect(module._to_ai_frame(static_due))

    assert observer.collector.selector.config.max_interval_seconds == 2.0
    assert first.processing.decision_reason == "initial_keyframe"
    assert change.selected_frame is not None
    assert change.processing.decision_reason in {"visual_change", "semantic_change"}
    assert due.selected_frame is not None
    assert due.processing.decision_reason == "max_interval"


def test_observer_second_round_sends_one_frame_and_bounded_previous_summary(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = SummaryRecordingAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        await observer.wait_idle()
        await observer.submit(_decoded_frame(tmp_path, sequence=2))
        await observer.wait_idle()

        assert len(adapter.requests) == 2
        assert len(adapter.requests[1].frame_refs) == 1
        assert adapter.requests[1].frame_refs[0].endswith("frame-000002.jpg")
        assert adapter.requests[1].memory_context == "s" * 2_000
        await observer.close()

    asyncio.run(scenario())


def test_observer_close_closes_private_adapter_exactly_once_and_tolerates_mock(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = CloseRecordingAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path / "private",
        )

        await observer.close()
        await observer.close()
        assert adapter.close_calls == 1

        mock_registry = ToolRegistry()
        mock_registry.register(VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()))
        mock_observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=mock_registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path / "mock",
        )
        await mock_observer.close()

    asyncio.run(scenario())


def test_observer_concurrent_close_waits_for_same_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = BlockingCloseAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path,
        )

        first = asyncio.create_task(observer.close())
        close_started = await asyncio.to_thread(adapter.close_started.wait, 1.0)
        assert close_started is True

        second = asyncio.create_task(observer.close())
        await asyncio.sleep(0)

        assert first.done() is False
        assert second.done() is False

        adapter.release_close.set()
        await asyncio.gather(first, second)
        assert adapter.close_calls == 1

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
        assert diagnostics.target_sequence == 1
        assert diagnostics.completed_sequence == 1
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


def test_observer_failure_keeps_last_success_and_projects_refreshing_then_stale(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        memory_module = importlib.import_module("assistant_agent.services.realtime_video_memory")
        adapter = SuccessThenBlockingFailureAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        memory = RealtimeVideoMemoryStore()
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        await observer.wait_idle()
        await observer.submit(_decoded_frame(tmp_path, sequence=2))
        assert await asyncio.to_thread(adapter.failure_started.wait, 2.0)

        refreshing = memory_module.project_realtime_video_context(
            memory.snapshot(VIDEO_ID), now_ms=5_000, target_sequence=2
        )
        assert refreshing.status == "refreshing"
        assert refreshing.summary == "first successful scene"

        adapter.release_failure.set()
        await observer.wait_idle()
        snapshot = memory.snapshot(VIDEO_ID)
        stale = memory_module.project_realtime_video_context(
            snapshot, now_ms=5_100, target_sequence=2
        )

        assert snapshot is not None
        assert snapshot.current_state == "first successful scene"
        assert snapshot.last_success_sequence == 1
        assert [frame.sequence for frame in snapshot.keyframes] == [1]
        assert stale.status == "stale"
        assert stale.error_code == "provider_timeout"
        assert stale.transport == "websocket"
        assert stale.session_generation == 2
        assert stale.connection_reused is False
        assert stale.reconnect_count == 1
        assert stale.completed_sequence is None
        assert stale.target_sequence == 2
        await observer.close()

    asyncio.run(scenario())


def test_observer_promotes_latest_frame_and_waits_for_target_snapshot_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = SequenceBlockingVideoAdapter((1, 3))
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
        assert await asyncio.to_thread(adapter.started[1].wait, 2.0)
        await observer.submit(_decoded_frame(tmp_path, sequence=2))
        promoted = await observer.promote(_decoded_frame(tmp_path, sequence=3))

        pending = memory.snapshot(VIDEO_ID)
        assert pending is not None
        assert pending.in_flight is True
        assert pending.pending_count == 1
        assert promoted.keyframe_selected is True
        assert "frame-000002.jpg" not in {
            path.name for path in keyframe_root.rglob("*.jpg")
        }

        waiter = asyncio.create_task(observer.wait_for_snapshot_sequence(3))
        await asyncio.sleep(0)
        assert not waiter.done()

        adapter.release[1].set()
        assert await asyncio.to_thread(adapter.completed[1].wait, 2.0)
        assert await asyncio.to_thread(adapter.started[3].wait, 2.0)
        await asyncio.sleep(0)
        assert not waiter.done()

        adapter.release[3].set()
        await asyncio.wait_for(waiter, 1.0)
        assert memory.snapshot(VIDEO_ID).last_success_sequence == 3
        assert adapter.sequences == [1, 3]
        await observer.close()

    asyncio.run(scenario())


def test_observer_sequence_wait_does_not_lose_update_before_await(tmp_path: Path) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        memory = RealtimeVideoMemoryStore()
        memory.mark_pending(VIDEO_ID, pending_count=1, in_flight=True)
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=ToolRegistry(),
            memory_store=memory,
            keyframe_root=tmp_path / "keyframes",
        )
        observer.video_id = VIDEO_ID
        original_snapshot = memory.snapshot
        first_snapshot = True

        def snapshot_with_boundary_success(video_id: str):
            nonlocal first_snapshot
            snapshot = original_snapshot(video_id)
            if first_snapshot:
                first_snapshot = False
                memory.record_success(
                    VIDEO_ID,
                    module.SemanticKeyframeRecord(
                        frame_id="frame-3",
                        uri="/tmp/frame-3.jpg",
                        sequence=3,
                    ),
                    VideoUnderstandingResult(
                        summary="frame 3",
                        provider="boundary-test",
                        output_ref="provider://video/test/3",
                    ),
                )
                observer._snapshot_updated.set()
            return snapshot

        memory.snapshot = snapshot_with_boundary_success
        try:
            await asyncio.wait_for(observer.wait_for_snapshot_sequence(3), 0.1)
        finally:
            memory.snapshot = original_snapshot
            await observer.close()

    asyncio.run(scenario())


def test_observer_concurrent_equal_sequence_promotion_retains_one_queued_artifact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = BlockingVideoAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(adapter.started.wait, 2.0)

        first_retain_started = threading.Event()
        second_retain_started = threading.Event()
        release_first_retain = threading.Event()
        retain_calls: list[int] = []
        original_retain = observer._retain_keyframe

        def racing_retain(frame: VideoFrame):
            if frame.sequence == 5:
                retain_calls.append(frame.sequence)
                if len(retain_calls) == 1:
                    first_retain_started.set()
                    assert release_first_retain.wait(timeout=2.0)
                else:
                    second_retain_started.set()
            return original_retain(frame)

        observer._retain_keyframe = racing_retain
        first = asyncio.create_task(observer.promote(_decoded_frame(tmp_path, sequence=5)))
        assert await asyncio.to_thread(first_retain_started.wait, 2.0)
        second = asyncio.create_task(observer.promote(_decoded_frame(tmp_path, sequence=5)))
        await asyncio.to_thread(second_retain_started.wait, 0.2)
        release_first_retain.set()
        await asyncio.gather(first, second)

        assert retain_calls == [5]
        assert observer.represented_sequence == 5
        assert observer._pending_item is not None
        assert Path(observer._pending_item.record.uri).is_file()

        adapter.release.set()
        await observer.wait_idle()
        assert adapter.sequences == [1, 5]
        await observer.close()

    asyncio.run(scenario())


def test_observer_close_waits_for_late_retention_and_cleans_ownership(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        keyframe_root = tmp_path / "keyframes"
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=ToolRegistry(),
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=keyframe_root,
        )
        retain_started = threading.Event()
        release_retain = threading.Event()
        original_retain = observer._retain_keyframe

        def late_retain(frame: VideoFrame):
            retain_started.set()
            assert release_retain.wait(timeout=2.0)
            return original_retain(frame)

        observer._retain_keyframe = late_retain
        promotion = asyncio.create_task(
            observer.promote(_decoded_frame(tmp_path, sequence=5))
        )
        assert await asyncio.to_thread(retain_started.wait, 2.0)

        close_task = asyncio.create_task(observer.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        release_retain.set()
        promotion_result = await asyncio.gather(promotion, return_exceptions=True)
        await close_task

        assert isinstance(promotion_result[0], RuntimeError)
        assert observer._owned_paths == set()
        assert not keyframe_root.exists()

    asyncio.run(scenario())


def test_observer_close_timeout_deletes_inflight_artifact_before_return(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = BlockingVideoAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        memory = RealtimeVideoMemoryStore()
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=memory,
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
            close_wait_seconds=0.01,
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(adapter.started.wait, 2.0)
        assert observer._inflight_item is not None
        retained_path = Path(observer._inflight_item.record.uri)
        late_execution = observer._execution_task
        assert retained_path.is_file()
        assert late_execution is not None

        await observer.close()

        assert not retained_path.exists()
        assert observer._owned_paths == set()
        assert memory.snapshot(VIDEO_ID) is None

        adapter.release.set()
        await asyncio.wait_for(asyncio.shield(late_execution), 1.0)
        assert not retained_path.exists()
        assert memory.snapshot(VIDEO_ID) is None

    asyncio.run(scenario())


def test_cancelled_adaptive_submit_retention_remains_observer_owned_until_close(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        keyframe_root = tmp_path / "keyframes"
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=ToolRegistry(),
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=keyframe_root,
            collector=AlwaysSelectCollector(),
        )
        retain_started = threading.Event()
        release_retain = threading.Event()
        retain_finished = threading.Event()
        original_retain = observer._retain_keyframe

        def blocked_retain(frame: VideoFrame):
            retain_started.set()
            assert release_retain.wait(timeout=2.0)
            retained = original_retain(frame)
            retain_finished.set()
            return retained

        observer._retain_keyframe = blocked_retain
        submission = asyncio.create_task(
            observer.submit(_decoded_frame(tmp_path, sequence=5))
        )
        assert await asyncio.to_thread(retain_started.wait, 2.0)
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission

        release_retain.set()
        assert await asyncio.to_thread(retain_finished.wait, 2.0)
        await asyncio.sleep(0)
        await observer.close()

        assert observer._owned_paths == set()
        assert observer._promotion_tasks == set()
        assert not keyframe_root.exists()

    asyncio.run(scenario())


def test_observer_partial_retention_failure_cleans_unregistered_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        keyframe_root = tmp_path / "keyframes"
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=ToolRegistry(),
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=keyframe_root,
        )

        def partial_copy(_source: str, destination: Path) -> None:
            Path(destination).write_bytes(b"partial")
            raise OSError("copy failed after creating destination")

        monkeypatch.setattr(module.shutil, "copy2", partial_copy)
        promotion = asyncio.create_task(
            observer.promote(_decoded_frame(tmp_path, sequence=5))
        )
        result = await asyncio.gather(promotion, return_exceptions=True)
        await observer.wait_for_promotions()
        await observer.close()

        assert isinstance(result[0], OSError)
        assert promotion.done()
        assert observer._owned_paths == set()
        assert not keyframe_root.exists()

    asyncio.run(scenario())


def test_observer_concurrent_promotion_cannot_replace_newer_pending_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        module = importlib.import_module("assistant_agent.services.realtime_video_observer")
        adapter = BlockingVideoAdapter()
        registry = ToolRegistry()
        registry.register(VideoUnderstandingTool(adapter=adapter))
        observer = module.RealtimeVideoObserver(
            user_id="user-1",
            session_id="session-1",
            registry=registry,
            memory_store=RealtimeVideoMemoryStore(),
            keyframe_root=tmp_path / "keyframes",
            collector=AlwaysSelectCollector(),
        )

        await observer.submit(_decoded_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(adapter.started.wait, 2.0)

        retain_started = threading.Event()
        release_older_retain = threading.Event()
        original_retain = observer._retain_keyframe

        def delayed_retain(frame: VideoFrame):
            if frame.sequence == 3:
                retain_started.set()
                assert release_older_retain.wait(timeout=2.0)
            return original_retain(frame)

        observer._retain_keyframe = delayed_retain
        older = asyncio.create_task(observer.promote(_decoded_frame(tmp_path, sequence=3)))
        assert await asyncio.to_thread(retain_started.wait, 2.0)
        newer = asyncio.create_task(observer.promote(_decoded_frame(tmp_path, sequence=5)))
        await asyncio.sleep(0)
        release_older_retain.set()
        await asyncio.gather(older, newer)

        assert observer.represented_sequence == 5
        adapter.release.set()
        await observer.wait_idle()
        assert adapter.sequences == [1, 5]
        await observer.close()

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
