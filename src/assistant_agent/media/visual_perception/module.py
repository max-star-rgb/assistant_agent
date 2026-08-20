"""Own realtime visual analysis resources behind one module boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.observability import LoggingEmbeddingObserver
from assistant_agent.media.embedding.provider import (
    create_multimodal_embedding_provider,
)
from assistant_agent.media.video.h264_video_ingestion import H264VideoIngestionService
from assistant_agent.media.video.qdrant_visual_memory_index import (
    create_visual_memory_text_index,
)
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_context import (
    REALTIME_VISUAL_TARGET_WINDOW_SIZE,
    SQLiteVideoContextStore,
    VideoContextStore,
    VideoFrame,
)
from assistant_agent.media.video.visual_memory_index import VisualMemoryTextIndex
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.media.vision.vision_client import (
    VisionUnderstandingClient,
    create_realtime_vision_understanding_client,
    create_vision_understanding_client,
)
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationService,
)
from assistant_agent.media.visual_perception.history_probe import (
    PoolVisualObservationHistoryProbe,
    VisualObservationHistoryProbe,
)


DEFAULT_VISUAL_PERCEPTION_ROOT = Path(".data") / "visual_perception"


class RealtimeVisualObserver(Protocol):
    async def submit(self, frame: VideoFrame) -> Any: ...

    async def promote(self, frame: VideoFrame) -> Any: ...

    async def promote_window(
        self,
        frames: Sequence[VideoFrame],
        *,
        window_id: str | None = None,
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
    ) -> Any: ...

    async def close(self) -> None: ...


ObserverFactory = Callable[[str, str], RealtimeVisualObserver]


@dataclass(frozen=True)
class VisualTarget:
    """One immutable frame boundary frozen when a chat starts."""

    video_id: str
    sequence: int


@dataclass(frozen=True)
class VisualTargetWindow:
    """Immutable decoded-frame boundary frozen when one chat starts."""

    window_id: str
    video_id: str
    start_sequence: int
    target_sequence: int
    sequences: tuple[int, ...]


@dataclass(frozen=True)
class LiveViewProjection:
    """Trusted live-view facts projected from the VLM side for one session.

    The main LLM never sees these (no``source=live_camera`` message block); the
    ``live_view_inspect`` Tool resolves them by ``(user_id, thread_id)`` from the
    process-owned visual module when it decides to answer a picture question.
    """

    live_video_ids: tuple[str, ...]
    window_id: str | None = None
    window_start_sequence: int | None = None
    target_sequence: int | None = None
    target_video_id: str | None = None


@dataclass(frozen=True)
class VisualPerceptionToolResources:
    """Read-side resources injected into governed Agent tools."""

    video_context_store: Any
    vision_client: VisionUnderstandingClient
    realtime_video_memory_store: RealtimeVideoMemoryStore
    visual_semantic_store_pool: SessionVisualSemanticStorePool
    visual_memory_text_index: VisualMemoryTextIndex
    visual_history_probe: VisualObservationHistoryProbe


class VisualPerceptionSession:
    """Connection-owned realtime analysis handle inside the visual module."""

    def __init__(
        self,
        *,
        observer: RealtimeVisualObserver,
        video_context_store: VideoContextStore,
        release: Callable[["VisualPerceptionSession"], None],
    ) -> None:
        self._observer = observer
        self._video_context_store = video_context_store
        self._release = release
        self._closed = False

    async def submit(self, frame: VideoFrame) -> Any:
        """Submit a decoded frame without waiting for its VLM result."""

        self._ensure_open()
        return await self._observer.submit(frame)

    async def prepare_strict_window(
        self,
        video_ids: Sequence[str],
    ) -> VisualTargetWindow | None:
        """Freeze the newest frame boundaries without starting VLM work."""

        self._ensure_open()
        selected: tuple[VideoFrame, ...] = ()
        for video_id in reversed(tuple(video_ids)):
            frames = tuple(
                await asyncio.to_thread(
                    self._video_context_store.get_recent_frames,
                    video_id,
                    limit=REALTIME_VISUAL_TARGET_WINDOW_SIZE,
                )
            )
            if frames:
                selected = frames
                break
        if not selected:
            return None
        _validate_target_window(selected)
        window_id = f"visual-window-{uuid4().hex}"
        return VisualTargetWindow(
            window_id=window_id,
            video_id=selected[-1].video_id,
            start_sequence=selected[0].sequence,
            target_sequence=selected[-1].sequence,
            sequences=tuple(frame.sequence for frame in selected),
        )

    async def prepare_strict_target(
        self,
        video_ids: Sequence[str],
    ) -> VisualTarget | None:
        """Freeze and promote the newest requested frame for strict inspection."""

        window = await self.prepare_strict_window(video_ids)
        if window is None:
            return None
        return VisualTarget(video_id=window.video_id, sequence=window.target_sequence)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._observer.close()
        finally:
            self._release(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("visual_perception_session_closed")


class VisualPerceptionModule:
    """Process-level owner for VLM, realtime observers, and visual semantics."""

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        data_root: Path | str = DEFAULT_VISUAL_PERCEPTION_ROOT,
        video_context_store: Any | None = None,
        realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
        visual_semantic_store_pool: SessionVisualSemanticStorePool | None = None,
        visual_memory_text_index: VisualMemoryTextIndex | None = None,
        observer_factory: ObserverFactory | None = None,
        vision_client: VisionUnderstandingClient | None = None,
    ) -> None:
        self.config = config or ProviderConfig.from_env()
        self.data_root = Path(data_root)
        self.video_context_store = video_context_store or SQLiteVideoContextStore()
        self.realtime_video_memory_store = (
            realtime_video_memory_store or RealtimeVideoMemoryStore()
        )
        self.embedding_observer = LoggingEmbeddingObserver()
        self.embedding_provider = create_multimodal_embedding_provider(self.config)
        self.embedding_coordinator_store = SessionEmbeddingCoordinatorStore(
            factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
                session_id,
                self.embedding_provider,
                observer=self.embedding_observer,
            )
        )
        self.visual_semantic_store_pool = visual_semantic_store_pool or (
            SessionVisualSemanticStorePool(
                root=self.data_root / "semantic",
                observer=self.embedding_observer,
            )
        )
        self.visual_memory_text_index = visual_memory_text_index or (
            create_visual_memory_text_index(self.config)
        )
        self.visual_history_probe = PoolVisualObservationHistoryProbe(
            self.visual_semantic_store_pool
        )
        self._vision_client = vision_client or _create_process_vision_client(
            self.config
        )
        self._vision_client_lock = Lock()
        self._observer_factory = observer_factory or self._create_observer
        self._sessions: set[VisualPerceptionSession] = set()
        self._live_views: dict[tuple[str, str], LiveViewProjection] = {}
        self._live_views_lock = Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def open_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> VisualPerceptionSession:
        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        if not user_id or not session_id:
            raise ValueError("visual perception identity must be non-empty")
        handle = VisualPerceptionSession(
            observer=self._observer_factory(user_id, session_id),
            video_context_store=self.video_context_store,
            release=self._sessions.discard,
        )
        self._sessions.add(handle)
        return handle

    def create_video_ingestion(self) -> H264VideoIngestionService:
        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        return H264VideoIngestionService(store=self.video_context_store)

    def record_live_view(
        self,
        user_id: str,
        session_id: str,
        *,
        video_ids: Sequence[str],
        window: VisualTargetWindow | None,
    ) -> None:
        """Freeze the current live-view facts for a session on the VLM side."""
        if self._closed:
            return
        projection = LiveViewProjection(
            live_video_ids=tuple(video_ids),
            window_id=window.window_id if window is not None else None,
            window_start_sequence=window.start_sequence if window is not None else None,
            target_sequence=window.target_sequence if window is not None else None,
            target_video_id=window.video_id if window is not None else None,
        )
        with self._live_views_lock:
            self._live_views[(user_id, session_id)] = projection

    def resolve_live_view(
        self,
        user_id: str,
        session_id: str,
    ) -> LiveViewProjection | None:
        """Return the session's current live-view facts, if any."""
        if not user_id or not session_id:
            return None
        with self._live_views_lock:
            return self._live_views.get((user_id, session_id))

    def understand(
        self,
        request: VisionUnderstandingRequest,
    ) -> VisionUnderstandingResult:
        """Run one explicit-media inference behind the module boundary."""

        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        if self._vision_client is None:
            raise RuntimeError("vision_understanding_client_unconfigured")
        with self._vision_client_lock:
            return self._vision_client.understand(request)

    def tool_resources(self) -> VisualPerceptionToolResources:
        return VisualPerceptionToolResources(
            video_context_store=self.video_context_store,
            vision_client=self,
            realtime_video_memory_store=self.realtime_video_memory_store,
            visual_semantic_store_pool=self.visual_semantic_store_pool,
            visual_memory_text_index=self.visual_memory_text_index,
            visual_history_probe=self.visual_history_probe,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions)
        if sessions:
            await asyncio.gather(
                *(session.aclose() for session in sessions),
                return_exceptions=True,
            )
        self.embedding_coordinator_store.close()
        self.visual_semantic_store_pool.close()
        close_index = getattr(self.visual_memory_text_index, "close", None)
        if callable(close_index):
            close_index()
        close_vision = getattr(self._vision_client, "close", None)
        if callable(close_vision):
            close_vision()

    def _create_observer(self, user_id: str, session_id: str) -> RealtimeVideoObserver:
        semantic_lease = self.visual_semantic_store_pool.acquire(user_id, session_id)
        try:
            embedding_lease = self.embedding_coordinator_store.acquire(
                user_id,
                session_id,
            )
        except Exception:
            semantic_lease.release()
            raise

        def release_resources() -> None:
            embedding_lease.release()
            semantic_lease.release()

        def observation_service_factory() -> RealtimeVisualObservationService:
            return RealtimeVisualObservationService(
                client=create_realtime_vision_understanding_client(self.config)
            )

        try:
            return RealtimeVideoObserver(
                user_id=user_id,
                session_id=session_id,
                observation_service_factory=observation_service_factory,
                memory_store=self.realtime_video_memory_store,
                semantic_store=semantic_lease.store,
                embedding_coordinator=embedding_lease.coordinator,
                visual_memory_text_index=self.visual_memory_text_index,
                provider_config=self.config,
                keyframe_root=self.data_root / "keyframes",
                resource_release=release_resources,
            )
        except Exception:
            release_resources()
            raise


_default_module: VisualPerceptionModule | None = None
_default_module_lock = Lock()


def get_visual_perception_module(
    config: ProviderConfig | None = None,
) -> VisualPerceptionModule:
    """Return the process-owned visual capability module."""

    global _default_module
    with _default_module_lock:
        if _default_module is None or _default_module.closed:
            _default_module = VisualPerceptionModule(config=config)
        return _default_module


def _create_process_vision_client(
    config: ProviderConfig,
) -> VisionUnderstandingClient | None:
    if config.provider_mode == "real" and (
        config.vision_provider == "mock"
        or config.resolved_vision_provider().missing_required_env()
    ):
        return None
    return create_vision_understanding_client(config)


def _validate_target_window(frames: Sequence[VideoFrame]) -> None:
    video_id = frames[0].video_id
    previous_sequence: int | None = None
    for frame in frames:
        if frame.video_id != video_id:
            raise ValueError("visual target window must contain one video id")
        if previous_sequence is not None and frame.sequence <= previous_sequence:
            raise ValueError("visual target window sequences must be strictly increasing")
        previous_sequence = frame.sequence
