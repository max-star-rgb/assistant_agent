"""Governed parallel background observation for realtime video keyframes."""

from __future__ import annotations

import asyncio
import shutil
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns, time
from typing import Any
from uuid import uuid4

from assistant_agent.config import VisionConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.observability import (
    VisionInferenceTraceLink,
    trace_visual_observation,
)
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationRequest,
    RealtimeVisualObservationService,
    RealtimeVisualObservationServiceFactory,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.semantic_pipeline import (
    SemanticFramePipeline,
)
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.keyframe.selector import (
    SemanticKeyframeConfig,
    SemanticKeyframeSelector,
)
from assistant_agent.media.video.types import (
    FrameProcessingResult,
    KeyframeChangeMetrics,
    VideoFrame as AIVideoFrame,
)
from assistant_agent.media.video.visual_reminder import (
    VisualReminderRegistry,
)
from assistant_agent.media.video.visual_memory_index import (
    UnavailableVisualMemoryTextIndex,
    VisualMemoryIndexDocument,
    VisualMemoryTextIndex,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KEYFRAME_ROOT = REPO_ROOT / ".data" / "agent_service_video_keyframes"
DEFAULT_CLOSE_WAIT_SECONDS = 1.0
KEYFRAME_WINDOW_SIZE = 5


@dataclass(frozen=True)
class _ObservationItem:
    records: tuple[SemanticKeyframeRecord, ...]
    enqueued_ns: int
    video_ingress_ns: int
    h264_decode_latency_ms: int | None
    keyframe_selection_latency_ms: int
    visual_window_id: str | None = None
    window_start_sequence: int | None = None
    target_sequence: int | None = None
    window_role: str = "background"

    @property
    def record(self) -> SemanticKeyframeRecord:
        return self.records[-1]


@dataclass(frozen=True)
class LogicalKeyframeWindowSnapshot:
    """Immutable selected-keyframe prefix belonging to one fixed window."""

    window_id: str
    video_id: str
    sequences: tuple[int, ...]
    timestamps_ms: tuple[int | None, ...] = ()

    @property
    def start_sequence(self) -> int:
        return self.sequences[0]

    @property
    def target_sequence(self) -> int:
        return self.sequences[-1]


@dataclass(frozen=True)
class _VisualSemanticPublishOutcome:
    visual_record_id: str
    visual_memory_index_latency_ms: int
    semantic_store_write_latency_ms: int


@dataclass(frozen=True)
class WindowPromotionResult:
    """Sequences newly enqueued or reused for one strict visual window."""

    enqueued_sequences: tuple[int, ...]
    reused_sequences: tuple[int, ...]


class RealtimeVideoObserver:
    """Select and analyze keyframes without blocking the media receive loop."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        observation_service_factory: RealtimeVisualObservationServiceFactory,
        memory_store: RealtimeVideoMemoryStore,
        semantic_store: SessionVisualSemanticStore | None = None,
        embedding_coordinator: SessionEmbeddingCoordinator,
        visual_reminder_registry: VisualReminderRegistry | None = None,
        visual_memory_text_index: VisualMemoryTextIndex | None = None,
        vision_config: VisionConfig,
        provider_mode: ProviderMode,
        keyframe_root: Path | str = DEFAULT_KEYFRAME_ROOT,
        close_wait_seconds: float = DEFAULT_CLOSE_WAIT_SECONDS,
        resource_release: Callable[[], None] | None = None,
        clock_ns: Callable[[], int] = perf_counter_ns,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if close_wait_seconds <= 0:
            raise ValueError("close_wait_seconds must be positive")
        self.user_id = user_id
        self.session_id = session_id
        self.observation_service_factory = observation_service_factory
        self.memory_store = memory_store
        self.embedding_coordinator = embedding_coordinator
        self.visual_reminder_registry = visual_reminder_registry
        self.visual_memory_text_index = (
            visual_memory_text_index
            or UnavailableVisualMemoryTextIndex(
                code="visual_memory_qdrant_unavailable",
                message="visual memory retrieval service is unavailable",
            )
        )
        self.keyframe_root = Path(keyframe_root)
        self.semantic_store = semantic_store or SessionVisualSemanticStore(
            root=self.keyframe_root / "semantic-store",
            session_id=session_id,
            observer=embedding_coordinator.observer,
        )
        self._owns_semantic_store = semantic_store is None
        self._resource_release = resource_release
        self._resources_released = False
        self._close_resources_finalized = False
        self.provider_mode = provider_mode
        self.close_wait_seconds = close_wait_seconds
        self.clock_ns = clock_ns
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))
        self.video_id: str | None = None
        self.closed = False
        self._observation_tasks: dict[int, asyncio.Task[None]] = {}
        self._observation_items: dict[int, _ObservationItem] = {}
        self._logical_keyframe_sequences: deque[int] = deque(maxlen=256)
        self._logical_keyframe_records: dict[int, SemanticKeyframeRecord] = {}
        self._open_logical_window_sequences: list[int] = []
        self._latest_closed_logical_window: LogicalKeyframeWindowSnapshot | None = None
        self._reserved_sequences: set[int] = set()
        self._attempted_window_targets: set[tuple[str, int]] = set()
        self._owned_paths: set[Path] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._first_terminal_snapshot = asyncio.Event()
        self._snapshot_updated = asyncio.Event()
        self._enqueue_lock = asyncio.Lock()
        self._promotion_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed_path_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._resource_finalization_task: asyncio.Task[None] | None = None
        self._semantic_pending_count = 0
        self._semantic_in_flight = False
        self.semantic_pipeline = SemanticFramePipeline(
            coordinator=embedding_coordinator,
            selector=SemanticKeyframeSelector(
                SemanticKeyframeConfig(
                    max_interval_seconds=(
                        vision_config.keyframe_max_interval_seconds
                    ),
                    semantic_threshold=(
                        vision_config.keyframe_semantic_threshold
                    ),
                )
            ),
            retention_root=self.keyframe_root / "semantic-input",
            on_embedded=self._handle_semantic_embedding,
            on_selected=self._handle_semantic_selection,
            on_state_change=self._update_semantic_pipeline_state,
            observer=embedding_coordinator.observer,
        )

    async def submit(self, frame: VideoFrame) -> FrameProcessingResult:
        """Submit one decoded frame to the low-latency semantic stage."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        return await self._run_owned_retention(self._submit(frame))

    async def _submit(self, frame: VideoFrame) -> FrameProcessingResult:
        self._accept_video_id(frame)
        started_ns = self.clock_ns()
        admission = await self.semantic_pipeline.submit(frame)
        finished_ns = self.clock_ns()
        return FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=_to_ai_frame(frame).timestamp_seconds,
            sampled=admission.admitted,
            sampling_rate=0.0,
            metrics=KeyframeChangeMetrics(),
            keyframe_selected=False,
            qwen_called=False,
            latency_ms=_elapsed_ms(started_ns, finished_ns),
            decision_reason=admission.reason,
            semantic_admission=admission.reason,
        )

    async def promote(self, frame: VideoFrame) -> FrameProcessingResult:
        """Enqueue a decoded frame without adaptive selection."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        return await self._run_owned_retention(self._promote(frame))

    async def promote_window(
        self,
        frames: Sequence[VideoFrame],
        *,
        window_id: str | None = None,
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
    ) -> WindowPromotionResult:
        """Enqueue one frozen frame window without adaptive latest-wins selection."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        selected = tuple(frames)
        if not selected:
            raise ValueError("realtime visual target window must not be empty")
        _validate_frame_window(selected)
        resolved_start = (
            selected[0].sequence
            if window_start_sequence is None
            else window_start_sequence
        )
        resolved_target = (
            selected[-1].sequence if target_sequence is None else target_sequence
        )
        if resolved_start != selected[0].sequence or resolved_target != selected[-1].sequence:
            raise ValueError("realtime visual window boundaries do not match frames")
        task = asyncio.create_task(
            self._promote_window(
                selected,
                window_id=window_id,
                window_start_sequence=resolved_start,
                target_sequence=resolved_target,
            )
        )
        self._promotion_tasks.add(task)
        task.add_done_callback(self._settle_owned_retention)
        return await asyncio.shield(task)

    async def _promote_window(
        self,
        frames: tuple[VideoFrame, ...],
        *,
        window_id: str | None,
        window_start_sequence: int,
        target_sequence: int,
    ) -> WindowPromotionResult:
        before = set(self._logical_keyframe_sequences)
        target = frames[-1]
        target_was_active = target.sequence in self._observation_tasks
        latest_sequence = (
            self._logical_keyframe_sequences[-1]
            if self._logical_keyframe_sequences
            else None
        )
        if latest_sequence is not None and any(
            frame.sequence <= latest_sequence
            and frame.sequence not in self._logical_keyframe_records
            for frame in frames
        ):
            raise ValueError("promoted window cannot insert historical keyframes")

        records: list[SemanticKeyframeRecord] = []
        for frame in frames:
            self._accept_video_id(frame)
            retained = self._logical_keyframe_records.get(frame.sequence)
            if retained is None:
                retained = await asyncio.to_thread(self._retain_keyframe, frame)
                await self._register_logical_keyframe(retained)
            records.append(retained)
        exact_window = LogicalKeyframeWindowSnapshot(
            window_id=window_id or f"visual-window-{window_start_sequence:08d}",
            video_id=frames[-1].video_id,
            sequences=tuple(record.sequence for record in records),
            timestamps_ms=tuple(record.timestamp_ms for record in records),
        )
        enqueued = await self.ensure_logical_keyframe_window(
            exact_window,
            window_role="target",
        )
        enqueued = enqueued or (
            not target_was_active
            and target.sequence in self._observation_tasks
        )
        newly_selected = tuple(
            frame.sequence for frame in frames if frame.sequence not in before
        )
        return WindowPromotionResult(
            enqueued_sequences=(target.sequence,) if enqueued else (),
            reused_sequences=(
                tuple(frame.sequence for frame in frames[:-1])
                if newly_selected
                else tuple(frame.sequence for frame in frames)
            ),
        )

    async def _run_owned_retention(
        self,
        operation,
    ) -> FrameProcessingResult:
        task = asyncio.create_task(operation)
        self._promotion_tasks.add(task)
        task.add_done_callback(self._settle_owned_retention)
        return await asyncio.shield(task)

    def _settle_owned_retention(
        self,
        task: asyncio.Task[Any],
    ) -> None:
        self._promotion_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _promote(self, frame: VideoFrame) -> FrameProcessingResult:
        self._accept_video_id(frame)
        started_ns = self.clock_ns()
        if self._sequence_is_represented(frame.sequence):
            return FrameProcessingResult(
                frame_id=frame.frame_id,
                timestamp_seconds=_to_ai_frame(frame).timestamp_seconds,
                sampled=True,
                sampling_rate=0.0,
                metrics=KeyframeChangeMetrics(),
                keyframe_selected=True,
                qwen_called=False,
                latency_ms=_elapsed_ms(started_ns, self.clock_ns()),
                decision_reason="already_represented",
                semantic_admission="already_represented",
            )
        represented = self.semantic_store.at_or_before(
            frame.video_id,
            sequence=frame.sequence,
        )
        if represented is not None and represented.frame_sequence == frame.sequence:
            return FrameProcessingResult(
                frame_id=frame.frame_id,
                timestamp_seconds=_to_ai_frame(frame).timestamp_seconds,
                sampled=True,
                sampling_rate=0.0,
                metrics=KeyframeChangeMetrics(),
                keyframe_selected=True,
                qwen_called=False,
                latency_ms=_elapsed_ms(started_ns, self.clock_ns()),
                decision_reason="already_represented",
                semantic_admission="already_represented",
            )
        admission = await self.semantic_pipeline.promote(frame)
        return FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=_to_ai_frame(frame).timestamp_seconds,
            sampled=True,
            sampling_rate=0.0,
            metrics=KeyframeChangeMetrics(),
            keyframe_selected=True,
            qwen_called=False,
            latency_ms=_elapsed_ms(started_ns, self.clock_ns()),
            decision_reason=admission.reason,
            semantic_admission=admission.reason,
        )

    async def _handle_semantic_selection(
        self,
        frame: VideoFrame,
        event: EmbeddingEvent | None,
        reason: str,
    ) -> None:
        """Register one selected keyframe and close each five-frame window."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        del event, reason
        record = SemanticKeyframeRecord(
            frame_id=frame.frame_id,
            uri=frame.uri,
            sequence=frame.sequence,
            timestamp_ms=frame.timestamp_ms,
        )
        if frame.sequence in self._logical_keyframe_records:
            await asyncio.to_thread(self._delete_transferred_duplicate, frame)
            return
        await self._register_logical_keyframe(record)
        self._open_logical_window_sequences.append(frame.sequence)
        if len(self._open_logical_window_sequences) == KEYFRAME_WINDOW_SIZE:
            window = self._close_open_logical_keyframe_window(frame.video_id)
            await self.ensure_logical_keyframe_window(
                window,
                window_role="background",
            )

    async def _register_logical_keyframe(
        self,
        record: SemanticKeyframeRecord,
    ) -> None:
        """Retain one selected record without assigning it to a rolling window."""

        if len(self._logical_keyframe_sequences) == self._logical_keyframe_sequences.maxlen:
            evicted_sequence = self._logical_keyframe_sequences.popleft()
            evicted = self._logical_keyframe_records.pop(evicted_sequence, None)
            if evicted is not None and not self._record_is_in_flight(evicted_sequence):
                await self._delete_record(evicted)
        self._logical_keyframe_sequences.append(record.sequence)
        self._logical_keyframe_records[record.sequence] = record
        self._owned_paths.add(Path(record.uri))

    async def _handle_semantic_embedding(
        self,
        frame: VideoFrame,
        event: EmbeddingEvent,
    ) -> None:
        """Compare reminders before keyframe selection using the shared embedding."""

        del frame
        if self.closed:
            return
        if self.visual_reminder_registry is not None:
            await self.visual_reminder_registry.publish_image_event(
                self.user_id,
                self.session_id,
                event,
            )

    async def _enqueue(
        self,
        frame: VideoFrame,
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
        already_retained: bool = False,
        visual_window_id: str | None = None,
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
        window_role: str = "background",
    ) -> bool:
        retained = (
            SemanticKeyframeRecord(
                frame_id=frame.frame_id,
                uri=frame.uri,
                sequence=frame.sequence,
                timestamp_ms=frame.timestamp_ms,
            )
            if already_retained
            else await asyncio.to_thread(self._retain_keyframe, frame)
        )
        self._owned_paths.add(Path(retained.uri))
        return await self._enqueue_records(
            (retained,),
            enqueued_ns=enqueued_ns,
            keyframe_selection_latency_ms=keyframe_selection_latency_ms,
            visual_window_id=visual_window_id,
            window_start_sequence=window_start_sequence,
            target_sequence=target_sequence,
            window_role=window_role,
        )

    async def _enqueue_records(
        self,
        records: tuple[SemanticKeyframeRecord, ...],
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
        visual_window_id: str | None = None,
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
        window_role: str = "background",
    ) -> bool:
        async with self._enqueue_lock:
            return await self._enqueue_serialized(
                records,
                enqueued_ns=enqueued_ns,
                keyframe_selection_latency_ms=keyframe_selection_latency_ms,
                visual_window_id=visual_window_id,
                window_start_sequence=window_start_sequence,
                target_sequence=target_sequence,
                window_role=window_role,
            )

    async def _enqueue_serialized(
        self,
        records: tuple[SemanticKeyframeRecord, ...],
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
        visual_window_id: str | None,
        window_start_sequence: int | None,
        target_sequence: int | None,
        window_role: str,
    ) -> bool:
        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        if not records or len(records) > KEYFRAME_WINDOW_SIZE:
            raise ValueError("realtime visual keyframe window must contain 1 to 5 frames")
        if any(
            current.sequence >= following.sequence
            for current, following in zip(records, records[1:])
        ):
            raise ValueError("realtime visual keyframe window must be increasing")
        sequence = records[-1].sequence
        attempt_key = (
            (visual_window_id, sequence)
            if visual_window_id is not None
            else None
        )
        if attempt_key is not None and attempt_key in self._attempted_window_targets:
            return False
        if self._sequence_is_represented(sequence):
            return False
        self._reserved_sequences.add(sequence)
        try:
            if self.closed:
                raise RuntimeError("realtime video observer is closed")
            if self.semantic_store.has_exact_sequence(
                self.video_id or "",
                sequence=sequence,
                visual_window_id=visual_window_id,
            ):
                return False
            snapshot = self.memory_store.snapshot(self.video_id or "")
            if snapshot is None or snapshot.last_success_sequence is None:
                self._first_terminal_snapshot.clear()
            target_frame = self._frame_for_record(records[-1])
            video_ingress_ns = _frame_timestamp_ns(target_frame, "video_ingress_ns")
            item = _ObservationItem(
                records=records,
                enqueued_ns=enqueued_ns,
                video_ingress_ns=(
                    video_ingress_ns if video_ingress_ns is not None else enqueued_ns
                ),
                h264_decode_latency_ms=_frame_latency_ms(
                    target_frame,
                    "h264_decode_latency_ms",
                ),
                keyframe_selection_latency_ms=keyframe_selection_latency_ms,
                visual_window_id=visual_window_id,
                window_start_sequence=window_start_sequence,
                target_sequence=target_sequence,
                window_role=window_role,
            )
            if attempt_key is not None:
                self._attempted_window_targets.add(attempt_key)
            try:
                task = asyncio.create_task(self._run_observation(item))
            except Exception:
                if attempt_key is not None:
                    self._attempted_window_targets.discard(attempt_key)
                raise
            self._observation_items[sequence] = item
            self._observation_tasks[sequence] = task
            task.add_done_callback(
                lambda completed, selected_sequence=sequence: self._settle_observation_task(
                    selected_sequence,
                    completed,
                )
            )
            self._idle.clear()
            self._update_pending_state()
            return True
        finally:
            self._reserved_sequences.discard(sequence)

    async def wait_idle(self) -> None:
        """Wait until semantic embedding, VLM, and reminder delivery are idle."""

        await self.semantic_pipeline.wait_idle()
        while self._observation_tasks:
            tasks = tuple(self._observation_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._idle.wait()
        if self.closed:
            await self._await_closed_resource_finalization()

    async def wait_for_first_terminal_snapshot(self) -> None:
        """Wait for the pending first observation, not later queued refreshes."""

        await self._first_terminal_snapshot.wait()

    async def wait_for_snapshot_sequence(self, sequence: int) -> None:
        """Wait until a successful snapshot reaches the requested sequence."""

        while not self.closed:
            self._snapshot_updated.clear()
            snapshot = self.memory_store.snapshot(self.video_id) if self.video_id else None
            if snapshot is not None and (snapshot.last_success_sequence or 0) >= sequence:
                return
            await self._snapshot_updated.wait()
        raise RuntimeError("realtime video observer is closed")

    async def wait_for_promotions(self) -> None:
        """Wait until all observer-owned promotion work has settled."""

        while self._promotion_tasks:
            tasks = tuple(self._promotion_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._promotion_tasks.difference_update(tasks)

    @property
    def represented_sequence(self) -> int | None:
        """Return the newest successful, in-flight, or pending sequence."""

        return self._represented_sequence()

    def recent_logical_keyframes(
        self,
        video_id: str,
        *,
        limit: int,
    ) -> tuple[int, ...]:
        """Return selected keyframe sequences regardless of VLM completion state."""

        if limit <= 0:
            raise ValueError("logical keyframe window limit must be positive")
        if self.video_id != video_id:
            return ()
        window = self.current_logical_keyframe_window(video_id)
        if window is None:
            return ()
        return window.sequences[-limit:]

    def current_logical_keyframe_window(
        self,
        video_id: str,
    ) -> LogicalKeyframeWindowSnapshot | None:
        """Return the open segment, or the latest closed segment when idle."""

        if self.video_id != video_id or not self._logical_keyframe_sequences:
            return None
        if not self._open_logical_window_sequences:
            return self._latest_closed_logical_window
        current = tuple(self._open_logical_window_sequences)
        return LogicalKeyframeWindowSnapshot(
            window_id=f"visual-window-{current[0]:08d}",
            video_id=video_id,
            sequences=current,
            timestamps_ms=tuple(
                self._logical_keyframe_records[sequence].timestamp_ms
                for sequence in current
            ),
        )

    def freeze_logical_keyframe_window(
        self,
        video_id: str,
    ) -> LogicalKeyframeWindowSnapshot | None:
        """Close the current segment at a user-input boundary."""

        if self.video_id != video_id or not self._logical_keyframe_sequences:
            return None
        if self._open_logical_window_sequences:
            return self._close_open_logical_keyframe_window(video_id)
        return self._latest_closed_logical_window

    def _close_open_logical_keyframe_window(
        self,
        video_id: str,
    ) -> LogicalKeyframeWindowSnapshot:
        sequences = tuple(self._open_logical_window_sequences)
        if not sequences:
            raise RuntimeError("logical keyframe window is already closed")
        window = LogicalKeyframeWindowSnapshot(
            window_id=f"visual-window-{sequences[0]:08d}",
            video_id=video_id,
            sequences=sequences,
            timestamps_ms=tuple(
                self._logical_keyframe_records[sequence].timestamp_ms
                for sequence in sequences
            ),
        )
        self._open_logical_window_sequences.clear()
        self._latest_closed_logical_window = window
        return window

    async def ensure_logical_keyframe_window(
        self,
        window: LogicalKeyframeWindowSnapshot,
        *,
        window_role: str,
    ) -> bool:
        """Start one immutable window snapshot, deduplicated by target sequence."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        if window.video_id != self.video_id:
            return False
        records = tuple(
            self._logical_keyframe_records[sequence]
            for sequence in window.sequences
            if sequence in self._logical_keyframe_records
        )
        if tuple(record.sequence for record in records) != window.sequences:
            return False
        return await self._enqueue_records(
            records,
            enqueued_ns=self.clock_ns(),
            keyframe_selection_latency_ms=0,
            visual_window_id=window.window_id,
            window_start_sequence=window.start_sequence,
            target_sequence=window.target_sequence,
            window_role=window_role,
        )

    def latest_keyframe_at_or_before(
        self,
        video_id: str,
        *,
        target_sequence: int,
    ) -> VideoFrame | None:
        """Return the nearest selected keyframe that cannot follow a chat boundary."""

        if isinstance(target_sequence, bool) or target_sequence < 0:
            return None
        candidates: list[SemanticKeyframeRecord] = []
        snapshot = self.memory_store.snapshot_at_or_before_sequence(
            video_id,
            target_sequence=target_sequence,
        )
        if snapshot is not None:
            candidates.extend(
                record
                for record in snapshot.keyframes
                if record.sequence <= target_sequence
            )
        for item in self._observation_items.values():
            if item.record.sequence <= target_sequence:
                candidates.append(item.record)
        candidates.extend(
            record
            for record in self._logical_keyframe_records.values()
            if record.sequence <= target_sequence
        )
        if not candidates:
            return None
        selected = max(candidates, key=lambda record: record.sequence)
        return VideoFrame(
            video_id=video_id,
            frame_id=selected.frame_id,
            uri=selected.uri,
            sequence=selected.sequence,
            timestamp_ms=selected.timestamp_ms,
            metadata={"source": "realtime_video_keyframe"},
        )

    async def close(self) -> None:
        """Stop work, reject late results, and remove owned semantic artifacts."""

        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        """Run close cleanup exactly once while concurrent callers await it."""

        if self.closed:
            return
        self.closed = True
        try:
            await self.semantic_pipeline.close(timeout_seconds=self.close_wait_seconds)
            await self.wait_for_promotions()
            active_tasks = tuple(self._observation_tasks.values())
            if active_tasks:
                await asyncio.wait(
                    active_tasks,
                    timeout=self.close_wait_seconds,
                )
            active_paths = {
                Path(record.uri)
                for item in self._observation_items.values()
                for record in item.records
            }
            for path in self._owned_paths - active_paths:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                self._owned_paths.discard(path)
            await asyncio.to_thread(_remove_empty_tree, self.keyframe_root)
            self._idle.set()
            self._snapshot_updated.set()
        finally:
            if not self._observation_tasks:
                await self._await_closed_resource_finalization()

    def _ensure_closed_resource_finalization(self) -> asyncio.Task[None]:
        if self._resource_finalization_task is None:
            self._resource_finalization_task = asyncio.create_task(
                self._finalize_closed_resources()
            )
        return self._resource_finalization_task

    async def _await_closed_resource_finalization(self) -> None:
        await asyncio.shield(self._ensure_closed_resource_finalization())

    async def _finalize_closed_resources(self) -> None:
        if self._close_resources_finalized:
            return
        while self._closed_path_cleanup_tasks:
            tasks = tuple(self._closed_path_cleanup_tasks)
            await asyncio.gather(*tasks)
        await asyncio.to_thread(self._finalize_closed_resources_sync)
        self._close_resources_finalized = True

    def _finalize_closed_resources_sync(self) -> None:
        if self.video_id is not None:
            self.memory_store.remove_video(self.video_id)
        if self._owns_semantic_store:
            self.semantic_store.close()
        for path in tuple(self._owned_paths):
            path.unlink(missing_ok=True)
            self._owned_paths.discard(path)
        _remove_empty_tree(self.keyframe_root)
        self._release_external_resources()

    async def _cleanup_closed_paths(self, paths: tuple[Path, ...]) -> None:
        await asyncio.to_thread(_unlink_paths, paths)
        for path in paths:
            self._owned_paths.discard(path)

    def _release_external_resources(self) -> None:
        if self._resources_released:
            return
        self._resources_released = True
        if self._resource_release is not None:
            self._resource_release()

    async def _run_observation(self, item: _ObservationItem) -> None:
        started_ns = self.clock_ns()
        observation_started_ns = started_ns
        self._update_pending_state()
        try:
            video_id = item_video_id(item.record, self.video_id)
            outcome, trace_link = await asyncio.to_thread(
                self._observe_with_isolated_service,
                RealtimeVisualObservationRequest(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    video_id=video_id,
                    frame_refs=tuple(record.uri for record in item.records),
                    frame_sequences=tuple(
                        record.sequence for record in item.records
                    ),
                    frame_timestamps_ms=tuple(
                        record.timestamp_ms for record in item.records
                    ),
                    visual_window_id=item.visual_window_id,
                    window_start_sequence=item.window_start_sequence,
                    target_sequence=item.target_sequence,
                    window_role=item.window_role,
                    provider_connection_isolated=True,
                ),
            )
            observation_finished_ns = self.clock_ns()
            if self.closed:
                return
            observation = outcome.result if outcome.succeeded else None
            if observation is not None:
                publish_outcome = await asyncio.to_thread(
                    self._publish_visual_semantic_record,
                    video_id,
                    item,
                    observation,
                    trace_link,
                )
                semantic_published_ns = self.clock_ns()
                diagnostics = self._observation_diagnostics(
                    provider=outcome.diagnostics,
                    item=item,
                    dequeued_ns=started_ns,
                    observation_started_ns=observation_started_ns,
                    observation_finished_ns=observation_finished_ns,
                    succeeded=True,
                    semantic_published_ns=semantic_published_ns,
                ).model_copy(
                    update={
                        "published_at_ms": self.wall_clock_ms(),
                        "text_embedding_latency_ms": None,
                        "visual_memory_index_latency_ms": (
                            publish_outcome.visual_memory_index_latency_ms
                        ),
                        "semantic_store_write_latency_ms": (
                            publish_outcome.semantic_store_write_latency_ms
                        ),
                    }
                )
                evicted = self.memory_store.record_success(
                    video_id,
                    item.record,
                    observation,
                    diagnostics=diagnostics,
                    source_vision_trace_id=(
                        trace_link.trace_id if trace_link is not None else None
                    ),
                    source_vision_run_id=(
                        trace_link.run_id if trace_link is not None else None
                    ),
                    source_vlm_span_id=(
                        trace_link.span_id if trace_link is not None else None
                    ),
                    source_visual_record_id=(
                        publish_outcome.visual_record_id
                    ),
                )
                for record in evicted:
                    await self._delete_record(record)
            else:
                error = outcome.error or {
                    "code": "realtime_video_snapshot_not_publishable",
                    "message": (
                        "Video observation result is not publishable as a "
                        "realtime video semantic snapshot."
                    ),
                    "recoverable": True,
                }
                self.semantic_store.record_failure(
                    video_id,
                    sequence=item.record.sequence,
                    error=error,
                )
                self.memory_store.record_failure(
                    video_id,
                    item.record,
                    error,
                    diagnostics=self._observation_diagnostics(
                        provider=outcome.diagnostics,
                        item=item,
                        dequeued_ns=started_ns,
                        observation_started_ns=observation_started_ns,
                        observation_finished_ns=observation_finished_ns,
                        succeeded=False,
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - background boundary.
            if not self.closed:
                video_id = item_video_id(item.record, self.video_id)
                error = {
                    "code": "video_observation_failed",
                    "message": sanitize_error_message(exc),
                    "recoverable": True,
                }
                self.semantic_store.record_failure(
                    video_id,
                    sequence=item.record.sequence,
                    error=error,
                )
                self.memory_store.record_failure(
                    video_id,
                    item.record,
                    error,
                )
        finally:
            self._first_terminal_snapshot.set()
            self._snapshot_updated.set()

    def _observe_with_isolated_service(
        self,
        request: RealtimeVisualObservationRequest,
    ):
        service: RealtimeVisualObservationService = self.observation_service_factory()
        try:
            return trace_visual_observation(
                lambda trace_context: service.observe(
                    request,
                    trace_context=trace_context,
                ),
                thread_id=self.session_id,
                frame_refs=request.frame_refs,
                frame_sequences=request.frame_sequences,
                frame_timestamps_ms=request.frame_timestamps_ms,
                visual_window_id=request.visual_window_id,
                window_start_sequence=request.window_start_sequence,
                target_sequence=request.frame_sequence,
                window_role=request.window_role,
                provider_connection_isolated=request.provider_connection_isolated,
                semantic_threshold=(
                    self.semantic_pipeline.selector.config.semantic_threshold
                ),
            )
        finally:
            service.close()

    def _settle_observation_task(
        self,
        sequence: int,
        task: asyncio.Task[None],
    ) -> None:
        item = self._observation_items.get(sequence)
        if self._observation_tasks.get(sequence) is task:
            self._observation_tasks.pop(sequence, None)
            self._observation_items.pop(sequence, None)
        if not task.cancelled():
            task.exception()
        if self.closed and item is not None:
            still_referenced = {
                Path(record.uri)
                for active_item in self._observation_items.values()
                for record in active_item.records
            }
            paths = tuple(
                Path(record.uri)
                for record in item.records
                if Path(record.uri) not in still_referenced
            )
            if paths:
                cleanup_task = asyncio.create_task(self._cleanup_closed_paths(paths))
                self._closed_path_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._closed_path_cleanup_tasks.discard)
        self._update_pending_state()
        if not self._observation_tasks:
            self._idle.set()
            if self.closed:
                self._ensure_closed_resource_finalization()

    def _publish_visual_semantic_record(
        self,
        video_id: str,
        item: _ObservationItem,
        result: VideoUnderstandingResult,
        trace_link: VisionInferenceTraceLink | None,
    ) -> _VisualSemanticPublishOutcome:
        frame = item.record
        record_id = f"visual-{uuid4().hex}"
        created_at_ms = self.wall_clock_ms()
        captured_at_ms = (
            frame.timestamp_ms if frame.timestamp_ms is not None else created_at_ms
        )
        index_started_ns = self.clock_ns()
        index_outcome = self.visual_memory_text_index.upsert(
            VisualMemoryIndexDocument(
                record_id=record_id,
                user_id=self.user_id,
                session_id=self.session_id,
                video_id=video_id,
                frame_sequence=frame.sequence,
                captured_at_ms=captured_at_ms,
                text=result.summary.strip() or _build_visual_search_text(result),
            )
        )
        index_finished_ns = self.clock_ns()
        evidence_bytes = Path(frame.uri).stat().st_size
        record = VisualSemanticRecord(
            record_id=record_id,
            session_id=self.session_id,
            video_id=video_id,
            frame_sequence=frame.sequence,
            visual_window_id=item.visual_window_id,
            window_start_sequence=item.window_start_sequence,
            window_sequences=tuple(record.sequence for record in item.records),
            captured_at_ms=captured_at_ms,
            summary=result.summary,
            scene=result.scene,
            objects=list(result.objects),
            people=list(result.people),
            actions=list(result.actions),
            events=list(result.events),
            changes=list(result.changes),
            uncertainties=list(result.uncertainties),
            text_in_video=list(result.text_in_video),
            products=list(result.products),
            brands=list(result.brands),
            colors=list(result.colors),
            materials=list(result.materials),
            timestamps=[dict(item) for item in result.timestamps],
            style_tags=list(result.style_tags),
            confidence=result.confidence,
            provider=result.provider,
            model=result.model,
            source_vision_trace_id=(
                trace_link.trace_id if trace_link is not None else None
            ),
            source_vision_run_id=(
                trace_link.run_id if trace_link is not None else None
            ),
            source_vlm_span_id=(
                trace_link.span_id if trace_link is not None else None
            ),
            search_embedding=None,
            embedding_space_id=None,
            index_status=index_outcome.status,
            evidence_ref=frame.uri,
            evidence_bytes=evidence_bytes,
            created_at_ms=created_at_ms,
        )
        store_started_ns = self.clock_ns()
        self.semantic_store.record_success(record)
        store_finished_ns = self.clock_ns()
        if index_outcome.status == "unavailable":
            emit_visual_semantic_observation(
                self.embedding_coordinator.observer,
                "visual_semantic.index_failed",
                session_id=self.session_id,
                sequence=frame.sequence,
                status="unavailable",
            )
        return _VisualSemanticPublishOutcome(
            visual_record_id=record.record_id,
            visual_memory_index_latency_ms=_elapsed_ms(
                index_started_ns,
                index_finished_ns,
            ),
            semantic_store_write_latency_ms=_elapsed_ms(
                store_started_ns,
                store_finished_ns,
            ),
        )

    def _observation_diagnostics(
        self,
        *,
        provider: dict[str, Any],
        item: _ObservationItem,
        dequeued_ns: int,
        observation_started_ns: int,
        observation_finished_ns: int,
        succeeded: bool,
        semantic_published_ns: int | None = None,
    ) -> RealtimeVideoObservationDiagnostics:
        return RealtimeVideoObservationDiagnostics(
            h264_decode_latency_ms=item.h264_decode_latency_ms,
            keyframe_selection_latency_ms=item.keyframe_selection_latency_ms,
            queue_wait_latency_ms=_elapsed_ms(item.enqueued_ns, dequeued_ns),
            observation_latency_ms=_elapsed_ms(
                observation_started_ns,
                observation_finished_ns,
            ),
            published_at_ns=semantic_published_ns,
            semantic_publish_latency_ms=(
                _elapsed_ms(item.video_ingress_ns, semantic_published_ns)
                if semantic_published_ns is not None
                else None
            ),
            transport=provider.get("transport"),
            session_generation=provider.get("session_generation"),
            connection_reused=provider.get("connection_reused"),
            reconnect_count=provider.get("reconnect_count"),
            target_sequence=provider.get("target_sequence", item.record.sequence),
            completed_sequence=provider.get(
                "completed_sequence",
                item.record.sequence if succeeded else None,
            ),
            first_delta_latency_ms=provider.get("first_delta_latency_ms"),
            total_observation_latency_ms=provider.get("total_observation_latency_ms"),
            jpeg_prepare_latency_ms=provider.get("jpeg_prepare_latency_ms"),
            connection_setup_latency_ms=provider.get("connection_setup_latency_ms"),
            instruction_update_latency_ms=provider.get("instruction_update_latency_ms"),
            media_commit_latency_ms=provider.get("media_commit_latency_ms"),
            response_first_delta_latency_ms=provider.get(
                "response_first_delta_latency_ms"
            ),
            response_tail_latency_ms=provider.get("response_tail_latency_ms"),
            response_latency_ms=provider.get("response_latency_ms"),
            result_parse_latency_ms=provider.get("result_parse_latency_ms"),
        )

    def _retain_keyframe(self, frame: VideoFrame) -> SemanticKeyframeRecord:
        suffix = _safe_name(frame.video_id.removeprefix("agent-service-video-"))
        directory = self.keyframe_root / suffix
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"frame-{frame.sequence:06d}-{uuid4().hex}.jpg"
        try:
            shutil.copy2(frame.uri, destination)
        except OSError:
            destination.unlink(missing_ok=True)
            _remove_empty_tree(self.keyframe_root)
            raise
        destination = destination.resolve()
        return SemanticKeyframeRecord(
            frame_id=frame.frame_id,
            uri=str(destination),
            sequence=frame.sequence,
            timestamp_ms=frame.timestamp_ms,
        )

    def _frame_for_record(self, record: SemanticKeyframeRecord) -> VideoFrame:
        return VideoFrame(
            video_id=self.video_id or "realtime-video",
            frame_id=record.frame_id,
            uri=record.uri,
            sequence=record.sequence,
            timestamp_ms=record.timestamp_ms,
            metadata={"source": "realtime_video_keyframe"},
        )

    def _record_is_in_flight(self, sequence: int) -> bool:
        return any(
            any(record.sequence == sequence for record in item.records)
            for item in self._observation_items.values()
        )

    async def _delete_record(
        self, record: SemanticKeyframeRecord | _ObservationItem
    ) -> None:
        if isinstance(record, _ObservationItem):
            record = record.record
        path = Path(record.uri)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        self._owned_paths.discard(path)

    def _update_pending_state(self) -> None:
        self._publish_pending_state()

    def _update_semantic_pipeline_state(
        self,
        pending_count: int,
        in_flight: bool,
    ) -> None:
        self._semantic_pending_count = pending_count
        self._semantic_in_flight = in_flight
        self._publish_pending_state()

    def _publish_pending_state(self) -> None:
        if self.video_id is None or self.closed:
            return
        pending_count = self._semantic_pending_count
        in_flight = self._semantic_in_flight or bool(self._observation_tasks)
        self.memory_store.mark_pending(
            self.video_id,
            pending_count=pending_count,
            in_flight=in_flight,
        )
        self.semantic_store.mark_pending(
            self.video_id,
            pending_count=pending_count,
            in_flight=in_flight,
        )
        self._snapshot_updated.set()

    def _accept_video_id(self, frame: VideoFrame) -> None:
        if self.video_id is None:
            self.video_id = frame.video_id
        elif self.video_id != frame.video_id:
            raise ValueError("realtime video observer accepts one video id")

    def _represented_sequence(self) -> int | None:
        snapshot = self.memory_store.snapshot(self.video_id) if self.video_id else None
        sequences = [
            snapshot.last_success_sequence if snapshot is not None else None,
            *self._observation_tasks,
        ]
        represented = [sequence for sequence in sequences if sequence is not None]
        return max(represented) if represented else None

    def _sequence_is_represented(self, sequence: int) -> bool:
        if sequence in self._reserved_sequences or sequence in self._observation_tasks:
            return True
        return self.semantic_store.has_exact_sequence(
            self.video_id or "",
            sequence=sequence,
        )

    def _delete_transferred_duplicate(self, frame: VideoFrame) -> None:
        path = Path(frame.uri)
        active_paths = {
            Path(record.uri)
            for item in self._observation_items.values()
            for record in item.records
        }
        logical_paths = {
            Path(record.uri) for record in self._logical_keyframe_records.values()
        }
        if path not in active_paths and path not in logical_paths:
            path.unlink(missing_ok=True)
            self._owned_paths.discard(path)


def _to_ai_frame(frame: VideoFrame) -> AIVideoFrame:
    timestamp_ms = frame.timestamp_ms if frame.timestamp_ms is not None else frame.sequence * 1000
    return AIVideoFrame(
        frame_id=frame.frame_id,
        timestamp_seconds=timestamp_ms / 1000.0,
        pixels=frame.fingerprint,
        uri=frame.uri,
        width=frame.fingerprint_width,
        height=frame.fingerprint_height,
        metadata={"video_id": frame.video_id, "sequence": frame.sequence},
    )


def _validate_frame_window(frames: Sequence[VideoFrame]) -> None:
    video_id = frames[0].video_id
    previous_sequence: int | None = None
    for frame in frames:
        if frame.video_id != video_id:
            raise ValueError("realtime visual target window must contain one video id")
        if previous_sequence is not None and frame.sequence <= previous_sequence:
            raise ValueError(
                "realtime visual target window sequences must be strictly increasing"
            )
        previous_sequence = frame.sequence


def _frame_latency_ms(frame: VideoFrame, key: str) -> int | None:
    metadata = frame.metadata
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _frame_timestamp_ns(frame: VideoFrame, key: str) -> int | None:
    metadata = frame.metadata
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, int((end_ns - start_ns) / 1_000_000))


def item_video_id(item: SemanticKeyframeRecord, video_id: str | None) -> str:
    _ = item
    if video_id is None:
        raise RuntimeError("video id is not initialized")
    return video_id


def _build_visual_search_text(result: VideoUnderstandingResult) -> str:
    # Only current-frame facts belong in the search embedding. Historical
    # comparisons and uncertain candidates must remain result metadata.
    sections = [
        ("场景", [result.scene] if result.scene else []),
        ("物体", result.objects),
        ("人物", result.people),
        ("动作", result.actions),
        ("事件", result.events),
        ("文字", result.text_in_video),
        ("摘要", [result.summary]),
    ]
    text = "\n".join(
        f"{label}：{'、'.join(value for value in values if value)}"
        for label, values in sections
        if any(values)
    )
    return text[:4_000] or "视觉记录"


def _safe_name(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return normalized or "video"


def _remove_empty_tree(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _unlink_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
