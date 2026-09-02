"""Own realtime visual analysis resources behind one module boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from assistant_agent.config import MediaConfig, VisionConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.provider import (
    MultimodalEmbeddingProvider,
    create_multimodal_embedding_provider,
)
from assistant_agent.media.video.h264_video_ingestion import H264VideoIngestionService
from assistant_agent.media.video.qdrant_visual_memory_index import (
    create_visual_memory_text_index,
)
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import (
    LogicalKeyframeWindowSnapshot,
    RealtimeVideoObserver,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_context import (
    InMemoryVideoContextStore,
    VideoContextStore,
    VideoFrame,
)
from assistant_agent.media.video.visual_reminder import (
    VisualReminderManager,
    VisualReminderRegistry,
)
from assistant_agent.media.video.visual_memory_index import VisualMemoryTextIndex
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.media.vision.vision_client import (
    VisionUnderstandingClient,
    create_vision_understanding_client,
)
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationService,
)
from assistant_agent.media.visual_perception.history_probe import (
    PoolVisualObservationHistoryProbe,
    VisualObservationHistoryProbe,
)
from assistant_agent.media.proactive_messages import ProactiveMessageSink
from assistant_agent.provider_mode import ProviderMode


DEFAULT_VISUAL_PERCEPTION_ROOT = Path(".data") / "visual_perception"


class RealtimeVisualObserver(Protocol):
    async def submit(self, frame: VideoFrame) -> Any: ...

    async def promote(self, frame: VideoFrame) -> Any: ...

    def recent_logical_keyframes(
        self,
        video_id: str,
        *,
        limit: int,
    ) -> tuple[int, ...]: ...

    def current_logical_keyframe_window(
        self,
        video_id: str,
    ) -> LogicalKeyframeWindowSnapshot | None: ...

    def freeze_logical_keyframe_window(
        self,
        video_id: str,
    ) -> LogicalKeyframeWindowSnapshot | None: ...

    async def ensure_logical_keyframe_window(
        self,
        window: LogicalKeyframeWindowSnapshot,
        *,
        window_role: str,
    ) -> bool: ...

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
    """Immutable logical-keyframe boundary frozen when one chat starts."""

    window_id: str
    video_id: str
    start_sequence: int
    target_sequence: int
    sequences: tuple[int, ...]
    timestamps_ms: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class LiveViewProjection:
    """Trusted live-view facts projected from the VLM side for one session.

    Raw projection fields do not enter the user message. Tool exposure and
    execution resolve them through one run-scoped capability.
    """

    live_video_ids: tuple[str, ...]
    window_id: str | None = None
    window_start_sequence: int | None = None
    target_sequence: int | None = None
    target_video_id: str | None = None
    window_sequences: tuple[int, ...] = ()
    window_timestamps_ms: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class VisualPerceptionToolResources:
    """Read-side resources injected into governed Agent tools."""

    video_context_store: Any
    vision_client: VisionUnderstandingClient
    realtime_video_memory_store: RealtimeVideoMemoryStore
    visual_semantic_store_pool: SessionVisualSemanticStorePool
    visual_memory_text_index: VisualMemoryTextIndex
    visual_history_probe: VisualObservationHistoryProbe
    embedding_coordinator_store: SessionEmbeddingCoordinatorStore | None
    visual_reminder_registry: VisualReminderRegistry | None


class VisualPerceptionSession:
    """Connection-owned realtime analysis handle inside the visual module."""

    def __init__(
        self,
        *,
        observer: RealtimeVisualObserver,
        video_context_store: VideoContextStore,
        reminder_manager: VisualReminderManager | None = None,
        reminder_registry: VisualReminderRegistry | None = None,
        reminder_sink: ProactiveMessageSink | None = None,
        release: Callable[["VisualPerceptionSession"], None],
    ) -> None:
        self._observer = observer
        self._video_context_store = video_context_store
        self._reminder_manager = reminder_manager
        self._reminder_registry = reminder_registry
        self._reminder_sink = reminder_sink
        self._reminder_registered = False
        self._release = release
        self._closed = False

    async def submit(self, frame: VideoFrame) -> Any:
        """Submit a decoded frame without waiting for its VLM result."""

        self._ensure_open()
        return await self._observer.submit(frame)

    def mark_video_received(self) -> None:
        """Activate connection-scoped reminders after the first decoded frame."""

        self._ensure_open()
        if self._reminder_registered:
            return
        if self._reminder_manager is None or self._reminder_registry is None:
            raise RuntimeError("visual_reminder_resources_unavailable")
        self._reminder_registry.register(
            self._reminder_manager,
            sink=self._reminder_sink,
        )
        self._reminder_registered = True

    def freeze_strict_window(
        self,
        video_ids: Sequence[str],
    ) -> VisualTargetWindow | None:
        """Synchronously linearize a chat against already-selected keyframes."""

        self._ensure_open()
        frozen: LogicalKeyframeWindowSnapshot | None = None
        for video_id in reversed(tuple(video_ids)):
            frozen = self._observer.freeze_logical_keyframe_window(video_id)
            if frozen is not None:
                break
        if frozen is None:
            return None
        selected = frozen.sequences
        _validate_logical_keyframe_window(selected)
        return VisualTargetWindow(
            window_id=frozen.window_id,
            video_id=frozen.video_id,
            start_sequence=selected[0],
            target_sequence=selected[-1],
            sequences=selected,
            timestamps_ms=frozen.timestamps_ms,
        )

    async def ensure_strict_window(self, window: VisualTargetWindow) -> bool:
        """Start the VLM for a window already frozen at chat arrival."""

        self._ensure_open()
        return await self._observer.ensure_logical_keyframe_window(
            LogicalKeyframeWindowSnapshot(
                window_id=window.window_id,
                video_id=window.video_id,
                sequences=window.sequences,
                timestamps_ms=window.timestamps_ms,
            ),
            window_role="target",
        )

    async def prepare_strict_window(
        self,
        video_ids: Sequence[str],
    ) -> VisualTargetWindow | None:
        """Freeze a logical window now and asynchronously start its VLM."""

        window = self.freeze_strict_window(video_ids)
        if window is not None:
            await self.ensure_strict_window(window)
        return window

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
            try:
                if (
                    self._reminder_registered
                    and self._reminder_manager is not None
                    and self._reminder_registry is not None
                ):
                    removed = await self._reminder_registry.close_connection(
                        self._reminder_manager.user_id,
                        self._reminder_manager.session_id,
                        manager=self._reminder_manager,
                    )
                    if not removed:
                        self._reminder_manager.close()
                elif self._reminder_manager is not None:
                    self._reminder_manager.close()
            finally:
                self._release(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("visual_perception_session_closed")


class VisualPerceptionModule:
    """Process-level owner for VLM, realtime observers, and visual semantics."""

    def __init__(
        self,
        *,
        provider_mode: ProviderMode,
        vision_config: VisionConfig,
        media_config: MediaConfig,
        data_root: Path | str = DEFAULT_VISUAL_PERCEPTION_ROOT,
        video_context_store: Any | None = None,
        realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
        visual_semantic_store_pool: SessionVisualSemanticStorePool | None = None,
        visual_memory_text_index: VisualMemoryTextIndex | None = None,
        observer_factory: ObserverFactory | None = None,
        vision_client: VisionUnderstandingClient | None = None,
        embedding_provider: MultimodalEmbeddingProvider | None = None,
    ) -> None:
        self.provider_mode = provider_mode
        self.vision_config = vision_config
        self.media_config = media_config
        self.data_root = Path(data_root)
        self.video_context_store = video_context_store or InMemoryVideoContextStore()
        self.realtime_video_memory_store = (
            realtime_video_memory_store or RealtimeVideoMemoryStore()
        )
        self.embedding_observer = None
        self.embedding_provider = (
            embedding_provider
            or create_multimodal_embedding_provider(
                self.vision_config,
                provider_mode=self.provider_mode,
            )
        )
        self.embedding_coordinator_store = SessionEmbeddingCoordinatorStore(
            factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
                session_id,
                self.embedding_provider,
                observer=self.embedding_observer,
            )
        )
        self.visual_reminder_registry = VisualReminderRegistry(
            delivery_timeout_seconds=(
                self.media_config.proactive_message_delivery_timeout_seconds
            ),
        )
        self.visual_semantic_store_pool = visual_semantic_store_pool or (
            SessionVisualSemanticStorePool(
                root=self.data_root / "semantic",
                observer=self.embedding_observer,
            )
        )
        self.visual_memory_text_index = visual_memory_text_index or (
            create_visual_memory_text_index(
                self.vision_config,
                provider_mode=self.provider_mode,
            )
        )
        self.visual_history_probe = PoolVisualObservationHistoryProbe(
            self.visual_semantic_store_pool
        )
        self._vision_client = vision_client or _create_process_vision_client(
            provider_mode=self.provider_mode,
            vision_config=self.vision_config,
            media_config=self.media_config,
        )
        self._vision_client_lock = Lock()
        self._observer_factory = observer_factory or self._create_observer
        self._sessions: set[VisualPerceptionSession] = set()
        self._frozen_live_views: dict[tuple[str, str, str], LiveViewProjection] = {}
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
        reminder_sink: ProactiveMessageSink | None = None,
    ) -> VisualPerceptionSession:
        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        if not user_id or not session_id:
            raise ValueError("visual perception identity must be non-empty")
        handle = VisualPerceptionSession(
            observer=self._observer_factory(user_id, session_id),
            video_context_store=self.video_context_store,
            reminder_manager=VisualReminderManager(
                user_id=user_id,
                session_id=session_id,
                similarity_threshold=(
                    self.vision_config.visual_reminder_similarity_threshold
                ),
                max_active=self.vision_config.visual_reminder_max_active,
                terminal_history_limit=(
                    self.vision_config.visual_reminder_terminal_history_limit
                ),
                observer=self.embedding_observer,
            ),
            reminder_registry=self.visual_reminder_registry,
            reminder_sink=reminder_sink,
            release=self._sessions.discard,
        )
        self._sessions.add(handle)
        return handle

    def create_video_ingestion(self) -> H264VideoIngestionService:
        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        return H264VideoIngestionService(store=self.video_context_store)

    def freeze_live_view(
        self,
        user_id: str,
        session_id: str,
        *,
        video_ids: Sequence[str],
        window: VisualTargetWindow | None,
    ) -> str | None:
        """Freeze one run's live-view facts and issue its opaque capability."""

        if self._closed or not user_id or not session_id or not video_ids:
            return None
        projection = LiveViewProjection(
            live_video_ids=tuple(video_ids),
            window_id=window.window_id if window is not None else None,
            window_start_sequence=window.start_sequence if window is not None else None,
            target_sequence=window.target_sequence if window is not None else None,
            target_video_id=window.video_id if window is not None else None,
            window_sequences=window.sequences if window is not None else (),
            window_timestamps_ms=(window.timestamps_ms if window is not None else ()),
        )
        with self._live_views_lock:
            token = uuid4().hex
            self._frozen_live_views[(user_id, session_id, token)] = projection
            return token

    def resolve_frozen_live_view(
        self,
        user_id: str,
        session_id: str,
        capability_token: str,
    ) -> LiveViewProjection | None:
        """Resolve one server-issued run projection after ownership checks."""

        if not user_id or not session_id or not capability_token:
            return None
        with self._live_views_lock:
            return self._frozen_live_views.get((user_id, session_id, capability_token))

    def release_frozen_live_view(
        self,
        user_id: str,
        session_id: str,
        capability_token: str,
    ) -> None:
        """Revoke one completed run's visual capability."""

        with self._live_views_lock:
            self._frozen_live_views.pop(
                (user_id, session_id, capability_token),
                None,
            )

    def understand(
        self,
        request: VisionUnderstandingRequest,
        *,
        config: RunnableConfig | None = None,
    ) -> VisionUnderstandingResult:
        """Run one explicit-media inference behind the module boundary."""

        if self._closed:
            raise RuntimeError("visual_perception_module_closed")
        if self._vision_client is None:
            raise RuntimeError("vision_understanding_client_unconfigured")
        with self._vision_client_lock:
            return self._vision_client.understand(request, config=config)

    @property
    def traces_as_chat_model(self) -> bool:
        return bool(
            self._vision_client is not None
            and getattr(self._vision_client, "traces_as_chat_model", False)
        )

    def tool_resources(self) -> VisualPerceptionToolResources:
        embedding_readiness = self.embedding_provider.readiness()
        reminder_ready = (
            embedding_readiness.image_ready and embedding_readiness.text_ready
        )
        return VisualPerceptionToolResources(
            video_context_store=self.video_context_store,
            vision_client=self,
            realtime_video_memory_store=self.realtime_video_memory_store,
            visual_semantic_store_pool=self.visual_semantic_store_pool,
            visual_memory_text_index=self.visual_memory_text_index,
            visual_history_probe=self.visual_history_probe,
            embedding_coordinator_store=(
                self.embedding_coordinator_store if reminder_ready else None
            ),
            visual_reminder_registry=(
                self.visual_reminder_registry if reminder_ready else None
            ),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._live_views_lock:
            self._frozen_live_views.clear()
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
                client=create_vision_understanding_client(
                    self.vision_config,
                    provider_mode=self.provider_mode,
                    media_config=self.media_config,
                )
            )

        try:
            return RealtimeVideoObserver(
                user_id=user_id,
                session_id=session_id,
                observation_service_factory=observation_service_factory,
                memory_store=self.realtime_video_memory_store,
                semantic_store=semantic_lease.store,
                embedding_coordinator=embedding_lease.coordinator,
                visual_reminder_registry=self.visual_reminder_registry,
                visual_memory_text_index=self.visual_memory_text_index,
                vision_config=self.vision_config,
                provider_mode=self.provider_mode,
                keyframe_root=self.data_root / "keyframes",
                resource_release=release_resources,
            )
        except Exception:
            release_resources()
            raise


_default_module: VisualPerceptionModule | None = None
_default_module_lock = Lock()


def get_visual_perception_module(
    *,
    provider_mode: ProviderMode,
    vision_config: VisionConfig,
    media_config: MediaConfig,
) -> VisualPerceptionModule:
    """Return the process-owned visual capability module."""

    global _default_module
    with _default_module_lock:
        if _default_module is None or _default_module.closed:
            _default_module = VisualPerceptionModule(
                provider_mode=provider_mode,
                vision_config=vision_config,
                media_config=media_config,
            )
        return _default_module


def _create_process_vision_client(
    *,
    provider_mode: ProviderMode,
    vision_config: VisionConfig,
    media_config: MediaConfig,
) -> VisionUnderstandingClient | None:
    if provider_mode == "real" and (
        vision_config.vision_provider == "mock"
        or vision_config.resolved_provider().missing_required_env()
    ):
        return None
    return create_vision_understanding_client(
        vision_config,
        provider_mode=provider_mode,
        media_config=media_config,
    )


def _validate_logical_keyframe_window(sequences: Sequence[int]) -> None:
    previous_sequence: int | None = None
    for sequence in sequences:
        if isinstance(sequence, bool) or sequence < 0:
            raise ValueError("visual target window sequence must be non-negative")
        if previous_sequence is not None and sequence <= previous_sequence:
            raise ValueError(
                "visual target window sequences must be strictly increasing"
            )
        previous_sequence = sequence
