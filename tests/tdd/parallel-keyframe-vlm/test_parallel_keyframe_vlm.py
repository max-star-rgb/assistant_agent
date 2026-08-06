from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

from assistant_agent.api import routes_agent
from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    PreparedChat,
    _create_realtime_video_observer,
    _protect_chat_visual_target,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    SemanticKeyframeRecord,
)
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
    VisionUnderstandingRequest,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _IndependentlyBlockedAdapter:
    provider = "parallel-sentinel"

    def __init__(self) -> None:
        self.started = {1: Event(), 2: Event()}
        self.release = {1: Event(), 2: Event()}
        self.started_sequences: list[int] = []
        self._lock = Lock()

    def understand_video(
        self,
        request: VideoUnderstandingRequest,
    ) -> VideoUnderstandingResult:
        sequence = int(request.metadata["frame_sequence"])
        with self._lock:
            self.started_sequences.append(sequence)
        self.started[sequence].set()
        if not self.release[sequence].wait(timeout=2.0):
            raise TimeoutError(f"sequence {sequence} was not released")
        return VideoUnderstandingResult(
            summary=f"frame-{sequence}-text",
            provider=self.provider,
            output_ref=f"parallel://frame/{sequence}",
        )


class _FailIfCalledVisionClient:
    def understand(self, request):
        raise AssertionError("live view must read the completed target text")


def _registry(adapter) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(RealtimeVideoObserveTool(video_adapter=adapter))
    registry.seal()
    return registry


def _frame(tmp_path: Path, *, sequence: int) -> VideoFrame:
    source = tmp_path / f"frame-{sequence}.jpg"
    source.write_bytes(f"frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-1",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 1_000,
    )


def test_later_keyframe_text_is_published_while_earlier_vlm_is_blocked(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_later_keyframe_is_not_blocked(tmp_path))


async def _assert_later_keyframe_is_not_blocked(tmp_path: Path) -> None:
    adapter = _IndependentlyBlockedAdapter()
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic-pool")
    semantic_store = semantic_pool.resolve("user-1", "session-1")
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_registry(adapter),
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider()
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(adapter.started[1].wait, 0.5)

        await observer.promote(_frame(tmp_path, sequence=2))
        assert await asyncio.to_thread(adapter.started[2].wait, 0.5)

        adapter.release[2].set()
        await asyncio.to_thread(
            observer.semantic_store.wait_for_sequence,
            "video-1",
            sequence=2,
            timeout_seconds=0.5,
        )
        record = observer.semantic_store.at_or_before("video-1", sequence=2)

        assert record is not None
        assert record.frame_sequence == 2
        assert record.summary == "frame-2-text"
        assert observer.semantic_store.at_or_before("video-1", sequence=1) is None

        inspect_result = await asyncio.to_thread(
            LiveViewInspectTool(
                client=_FailIfCalledVisionClient(),
                semantic_store_pool=semantic_pool,
            ).run,
            VisionUnderstandingRequest(video_ids=["video-1"]),
            ToolContext(
                user_id="user-1",
                session_id="session-1",
                metadata={
                    "request_metadata": {
                        "transport": "agent_service_websocket",
                        "gateway": {
                            "session_config": {"entry_profile": "agent_service"}
                        },
                        "agent_service": {"visual_target_sequence": 2},
                    }
                },
            ),
        )
        inspect_observation = observation_from_tool_result(inspect_result)
        assert inspect_observation.summary == "frame-2-text"
        assert inspect_observation.data["observations"][-1] == {
            "timestamp_ms": 2_000,
            "text": "frame-2-text",
        }
    finally:
        adapter.release[1].set()
        adapter.release[2].set()
        await observer.wait_idle()
        await observer.close()
        semantic_pool.close()


def test_chat_freezes_latest_selected_keyframe_before_raw_frame_at_a(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_chat_freezes_latest_selected_keyframe(tmp_path))


async def _assert_chat_freezes_latest_selected_keyframe(tmp_path: Path) -> None:
    selected = _frame(tmp_path, sequence=2)
    latest_raw = _frame(tmp_path, sequence=3)
    memory_store = RealtimeVideoMemoryStore()
    memory_store.record_success(
        "video-1",
        SemanticKeyframeRecord(
            frame_id=selected.frame_id,
            uri=selected.uri,
            sequence=selected.sequence,
            timestamp_ms=selected.timestamp_ms,
        ),
        VideoUnderstandingResult(
            summary="frame-2-text",
            provider="sentinel",
            output_ref="sentinel://frame/2",
        ),
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_registry(FakeRealtimeVisionAdapter()),
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider()
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    observer.video_id = "video-1"
    prepared = PreparedChat(
        session_id="session-1",
        response_session_id="protocol-session",
        body={},
        chat_index="chat-1",
        user_number="user-1",
        latest_speech="这是什么",
        contents=[],
        video_ids=["video-1"],
        received_ns=1,
        accepted_ns=2,
        session_turn=1,
        video_target_frame=latest_raw,
    )
    state = AgentServiceConnectionState(
        session_id="protocol-session",
        runtime_session_id="session-1",
        query_params={},
        video_observer=observer,
    )
    try:
        protected = await _protect_chat_visual_target(state=state, prepared=prepared)

        assert protected.video_target_frame is not None
        assert protected.video_target_frame.sequence == 2
        assert protected.visual_target_lease is not None
        assert protected.visual_target_lease.sequence == 2
    finally:
        if "protected" in locals() and protected.visual_target_lease is not None:
            protected.visual_target_lease.release()
        await observer.wait_idle()
        await observer.close()


def test_production_builds_an_independent_vlm_registry_for_each_keyframe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asyncio.run(_assert_production_builds_independent_registries(tmp_path, monkeypatch))


async def _assert_production_builds_independent_registries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assistant_agent.tools.plugins import registry_factory

    adapters: list[_CapturingAdapter] = []

    def build_registry(*_args, **_kwargs) -> ToolRegistry:
        adapter = _CapturingAdapter()
        adapters.append(adapter)
        return _registry(adapter)

    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic-pool")
    embedding_store = SessionEmbeddingCoordinatorStore(
        factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
            session_id,
            MockMultimodalEmbeddingProvider(),
        )
    )
    runtime = SimpleNamespace(
        config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        realtime_video_memory_store=RealtimeVideoMemoryStore(),
        visual_semantic_store_pool=semantic_pool,
        embedding_coordinator_store=embedding_store,
        visual_reminder_registry=None,
    )
    monkeypatch.setattr(
        routes_agent,
        "get_assistant_runtime_app",
        lambda: SimpleNamespace(runtime=runtime),
    )
    monkeypatch.setattr(
        registry_factory,
        "create_realtime_video_observation_registry",
        build_registry,
    )

    observer = _create_realtime_video_observer(
        user_id="user-production",
        session_id="session-production",
        state=AgentServiceConnectionState(
            session_id="session-production",
            query_params={},
        ),
    )
    try:
        await observer.promote(_frame(tmp_path, sequence=1))
        await observer.promote(_frame(tmp_path, sequence=2))
        await observer.wait_idle()

        assert len(adapters) == 2
        assert [adapter.sequences for adapter in adapters] == [[1], [2]]
    finally:
        await observer.close()
        embedding_store.close()
        semantic_pool.close()


class _CapturingAdapter:
    provider = "registry-sentinel"

    def __init__(self) -> None:
        self.sequences: list[int] = []

    def understand_video(
        self,
        request: VideoUnderstandingRequest,
    ) -> VideoUnderstandingResult:
        sequence = int(request.metadata["frame_sequence"])
        self.sequences.append(sequence)
        return VideoUnderstandingResult(
            summary=f"frame-{sequence}-text",
            provider=self.provider,
            output_ref=f"registry://frame/{sequence}",
        )


def test_one_registry_creation_failure_does_not_block_later_keyframes(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_registry_creation_failure_is_isolated(tmp_path))


async def _assert_registry_creation_failure_is_isolated(tmp_path: Path) -> None:
    attempts = 0

    def build_registry() -> ToolRegistry:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("registry-construction-sentinel")
        return _registry(_CapturingAdapter())

    memory_store = RealtimeVideoMemoryStore()
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=None,
        observation_registry_factory=build_registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider()
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(_frame(tmp_path, sequence=1))
        await asyncio.sleep(0)
        await observer.promote(_frame(tmp_path, sequence=2))
        await observer.wait_idle()

        assert memory_store.sequence_failed("video-1", target_sequence=1)
        record = observer.semantic_store.at_or_before("video-1", sequence=2)
        assert record is not None
        assert record.frame_sequence == 2
    finally:
        await observer.close()


def test_keyframe_selection_latency_excludes_h264_decode_time(tmp_path: Path) -> None:
    asyncio.run(_assert_keyframe_selection_latency_is_measured(tmp_path))


async def _assert_keyframe_selection_latency_is_measured(tmp_path: Path) -> None:
    memory_store = RealtimeVideoMemoryStore()
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_registry(_CapturingAdapter()),
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider()
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
        clock_ns=lambda: 300_000_000,
    )
    frame = replace(
        _frame(tmp_path, sequence=1),
        metadata={
            "video_ingress_ns": 0,
            "h264_decode_latency_ms": 100,
        },
    )
    try:
        await observer.promote(frame)
        await observer.wait_idle()
        snapshot = memory_store.snapshot("video-1")

        assert snapshot is not None
        assert snapshot.observation_diagnostics is not None
        assert snapshot.observation_diagnostics.keyframe_selection_latency_ms == 200
    finally:
        await observer.close()
