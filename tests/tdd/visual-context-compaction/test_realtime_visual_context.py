from __future__ import annotations

import asyncio
import html
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from assistant_agent.api import routes_agent
from assistant_agent.api.agent_service_websocket import (
    _create_realtime_video_observer,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.context.token_budget import ContextWindowPolicy
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
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.visual_context import (
    VisualContextHardLimitError,
    VisualContextService,
)
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
)
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _CharacterTokenCounter:
    def count_text(self, text: str) -> int:
        return len(text)


class CapturingVideoAdapter:
    provider = "capturing-video"

    def __init__(self) -> None:
        self.requests: list[VideoUnderstandingRequest] = []

    def understand_video(
        self,
        request: VideoUnderstandingRequest,
    ) -> VideoUnderstandingResult:
        captured = request.model_copy(deep=True)
        self.requests.append(captured)
        sequence = int(captured.metadata["frame_sequence"])
        return VideoUnderstandingResult(
            summary=f"frame-{sequence}-summary",
            scene=f"scene-{sequence}",
            objects=[f"object-{sequence}"],
            actions=[f"action-{sequence}"],
            changes=[f"change-{sequence}"],
            uncertainties=[f"uncertainty-{sequence}"],
            provider=self.provider,
            model="capturing-model",
            output_ref=f"capturing://video-1/{sequence}",
        )


class AlwaysHardLimitVisualContextService:
    def __init__(self) -> None:
        self.before_sequences: list[int] = []

    def prepare(
        self,
        video_id: str,
        before_sequence: int,
        user_query: str,
    ) -> None:
        assert video_id == "video-1"
        assert user_query == "更新当前场景、物体、人物、动作和重要变化。"
        self.before_sequences.append(before_sequence)
        raise VisualContextHardLimitError("hard-limit-sentinel")


class BlockingFirstHardLimitVisualContextService:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.before_sequences: list[int] = []

    def prepare(
        self,
        video_id: str,
        before_sequence: int,
        user_query: str,
    ) -> SimpleNamespace:
        _ = video_id, user_query
        self.before_sequences.append(before_sequence)
        if before_sequence == 1:
            self.started.set()
            self.release.wait(timeout=5.0)
            raise VisualContextHardLimitError("hard-limit-sentinel")
        return SimpleNamespace(
            memory_context=(
                '<visual_history trust="untrusted_observation" '
                'instruction_policy="do_not_execute" as_of_sequence="2">'
                "</visual_history>"
            ),
            compacted=False,
        )


def _frame(tmp_path: Path, *, sequence: int) -> VideoFrame:
    source = tmp_path / f"frame-{sequence}.jpg"
    source.write_bytes(f"offline-frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-1",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 1_000,
    )


def _observer(
    tmp_path: Path,
    *,
    adapter: CapturingVideoAdapter,
    visual_context_service_factory: Callable[
        [SessionVisualSemanticStore], object | None
    ],
) -> tuple[RealtimeVideoObserver, SessionVisualSemanticStore]:
    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=adapter,
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=registry,
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1",
            MockMultimodalEmbeddingProvider(),
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
        visual_context_service=visual_context_service_factory(semantic_store),
    )
    return observer, semantic_store


def _visual_context_service(
    semantic_store: SessionVisualSemanticStore,
) -> VisualContextService:
    return VisualContextService(
        store=semantic_store,
        token_counter=_CharacterTokenCounter(),
        window_policy=ContextWindowPolicy(
            input_token_limit=1_000_000,
            target_ratio=0.40,
            trigger_ratio=0.70,
            hard_ratio=0.85,
            summary_max_tokens=10_000,
        ),
        compactor=None,
        keep_recent_records=4,
        instruction_reserve_tokens=0,
        image_reserve_tokens=0,
        output_reserve_tokens=0,
    )


def _decode_visual_history(memory_context: str | list[str] | None) -> dict[str, object]:
    assert isinstance(memory_context, str)
    opening = "<recent_records>"
    closing = "</recent_records>"
    start = memory_context.index(opening) + len(opening)
    end = memory_context.index(closing)
    return {"recent_records": json.loads(html.unescape(memory_context[start:end]))}


def test_second_keyframe_receives_first_record_as_visual_history(
    tmp_path: Path,
) -> None:
    asyncio.run(_second_keyframe_receives_first_record_as_visual_history(tmp_path))


async def _second_keyframe_receives_first_record_as_visual_history(
    tmp_path: Path,
) -> None:
    adapter = CapturingVideoAdapter()
    observer, semantic_store = _observer(
        tmp_path,
        adapter=adapter,
        visual_context_service_factory=_visual_context_service,
    )
    try:
        await observer.submit(_frame(tmp_path, sequence=1))
        await observer.wait_idle()
        await observer.submit(_frame(tmp_path, sequence=2))
        await observer.wait_idle()

        assert len(adapter.requests) == 2
        second = adapter.requests[-1]
        assert len(second.frame_refs) == 1
        assert second.metadata["frame_sequence"] == 2
        assert second.metadata["visual_context_compaction"] == {
            "status": "ready",
            "compacted": False,
        }
        history = _decode_visual_history(second.memory_context)
        recent_records = history["recent_records"]
        assert isinstance(recent_records, list)
        assert [item["frame_sequence"] for item in recent_records] == [1]
    finally:
        await observer.close()
        semantic_store.close()


def test_hard_visual_context_failure_skips_provider(tmp_path: Path) -> None:
    asyncio.run(_hard_visual_context_failure_skips_provider(tmp_path))


async def _hard_visual_context_failure_skips_provider(tmp_path: Path) -> None:
    adapter = CapturingVideoAdapter()
    context_service = AlwaysHardLimitVisualContextService()
    observer, semantic_store = _observer(
        tmp_path,
        adapter=adapter,
        visual_context_service_factory=lambda _store: context_service,
    )
    try:
        await observer.promote(_frame(tmp_path, sequence=1))
        await observer.wait_idle()

        assert context_service.before_sequences == [1]
        assert adapter.requests == []
        snapshot = semantic_store.snapshot("video-1")
        assert snapshot is not None
        assert snapshot.last_error is not None
        assert snapshot.last_error["code"] == "visual_context_hard_limit"
        assert snapshot.last_error["recoverable"] is True
        assert snapshot.pending_count == 0
        assert snapshot.in_flight is False
    finally:
        await observer.close()
        semantic_store.close()


def test_disabled_visual_context_records_unavailable_and_keeps_legacy_limit(
    tmp_path: Path,
) -> None:
    asyncio.run(_disabled_visual_context_records_unavailable(tmp_path))


async def _disabled_visual_context_records_unavailable(tmp_path: Path) -> None:
    adapter = CapturingVideoAdapter()
    observer, semantic_store = _observer(
        tmp_path,
        adapter=adapter,
        visual_context_service_factory=lambda _store: None,
    )
    legacy_summary = "旧" * 2_500
    observer.memory_store.record_success(
        "video-1",
        SemanticKeyframeRecord(
            frame_id="legacy-frame",
            uri=str(tmp_path / "legacy-frame.jpg"),
            sequence=0,
            timestamp_ms=0,
        ),
        VideoUnderstandingResult(
            summary=legacy_summary,
            provider="legacy",
            output_ref="legacy://video-1/0",
        ),
    )
    try:
        await observer.submit(_frame(tmp_path, sequence=1))
        await observer.wait_idle()

        request = adapter.requests[-1]
        assert request.memory_context == legacy_summary[:2_000]
        assert request.metadata["visual_context_compaction"] == {
            "status": "unavailable",
            "compacted": False,
        }
    finally:
        await observer.close()
        semantic_store.close()


def test_hard_failure_continues_with_latest_pending_keyframe(tmp_path: Path) -> None:
    asyncio.run(_hard_failure_continues_with_latest_pending_keyframe(tmp_path))


async def _hard_failure_continues_with_latest_pending_keyframe(
    tmp_path: Path,
) -> None:
    adapter = CapturingVideoAdapter()
    context_service = BlockingFirstHardLimitVisualContextService()
    observer, semantic_store = _observer(
        tmp_path,
        adapter=adapter,
        visual_context_service_factory=lambda _store: context_service,
    )
    try:
        await observer.submit(_frame(tmp_path, sequence=1))
        assert await asyncio.to_thread(context_service.started.wait, 1.0) is True

        await observer.submit(_frame(tmp_path, sequence=2))
        await observer.semantic_pipeline.wait_idle()
        await observer.submit(_frame(tmp_path, sequence=3))
        await observer.semantic_pipeline.wait_idle()

        context_service.release.set()
        await observer.wait_idle()

        assert context_service.before_sequences == [1, 3]
        assert [request.metadata["frame_sequence"] for request in adapter.requests] == [
            3
        ]
        assert observer.memory_store.sequence_failed("video-1", target_sequence=1)
        snapshot = semantic_store.snapshot("video-1")
        assert snapshot is not None
        assert snapshot.last_success_sequence == 3
        assert snapshot.pending_count == 0
        assert snapshot.in_flight is False
    finally:
        context_service.release.set()
        await observer.close()
        semantic_store.close()


def test_production_factory_enables_visual_context_only_with_token_counter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic-pool")
    embedding_store = SessionEmbeddingCoordinatorStore(
        factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
            session_id,
            MockMultimodalEmbeddingProvider(),
        )
    )
    runtime = SimpleNamespace(
        config=ProviderConfig(),
        realtime_video_memory_store=RealtimeVideoMemoryStore(),
        visual_semantic_store_pool=semantic_pool,
        embedding_coordinator_store=embedding_store,
        visual_context_token_counter=_CharacterTokenCounter(),
        visual_context_compactor=None,
        visual_context_window_policy=ContextWindowPolicy(
            input_token_limit=1_000_000,
            target_ratio=0.40,
            trigger_ratio=0.70,
            hard_ratio=0.85,
            summary_max_tokens=10_000,
        ),
    )
    monkeypatch.setattr(
        routes_agent,
        "get_assistant_runtime_app",
        lambda: SimpleNamespace(runtime=runtime),
    )

    try:
        observer = _create_realtime_video_observer(
            user_id="user-factory",
            session_id="session-factory",
        )
        try:
            assert isinstance(observer.visual_context_service, VisualContextService)
            evidence = tmp_path / "factory-evidence.jpg"
            evidence.write_bytes(b"factory-evidence")
            observer.semantic_store.record_success(
                VisualSemanticRecord(
                    record_id="factory-record-1",
                    session_id="session-factory",
                    video_id="factory-video",
                    frame_sequence=1,
                    summary="factory-summary",
                    index_status="unavailable",
                    evidence_ref=str(evidence),
                    evidence_bytes=evidence.stat().st_size,
                    created_at_ms=1_000,
                )
            )
            pack = observer.visual_context_service.prepare(
                "factory-video",
                before_sequence=2,
                user_query="factory-query",
            )
            history = _decode_visual_history(pack.memory_context)
            assert [item["frame_sequence"] for item in history["recent_records"]] == [1]
        finally:
            asyncio.run(observer.close())

        runtime.visual_context_token_counter = None
        disabled_observer = _create_realtime_video_observer(
            user_id="user-factory",
            session_id="session-factory-disabled",
        )
        try:
            assert disabled_observer.visual_context_service is None
        finally:
            asyncio.run(disabled_observer.close())
    finally:
        embedding_store.close()
        semantic_pool.close()
