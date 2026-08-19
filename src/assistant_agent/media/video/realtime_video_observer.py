"""Governed bounded background observation for realtime video keyframes."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns, time
from typing import Any
from uuid import uuid4

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
)
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.observability import VisionInferenceTraceLink
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationRequest,
    RealtimeVisualObservationService,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.semantic_pipeline import (
    FixedIntervalSemanticSampler,
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
REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class _ObservationItem:
    record: SemanticKeyframeRecord
    enqueued_ns: int
    video_ingress_ns: int
    h264_decode_latency_ms: int | None
    keyframe_selection_latency_ms: int


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


@dataclass(frozen=True)
class _BackgroundVisionTraceContext:
    trace_store: TraceStore
    trace_id: str
    run_id: str
    user_id: str
    session_id: str
    parent_span_id: str | None = None


class RealtimeVideoObserver:
    """Select and analyze keyframes without blocking the media receive loop."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        observation_service: RealtimeVisualObservationService,
        memory_store: RealtimeVideoMemoryStore,
        semantic_store: SessionVisualSemanticStore | None = None,
        embedding_coordinator: SessionEmbeddingCoordinator,
        visual_reminder_registry: VisualReminderRegistry | None = None,
        visual_memory_text_index: VisualMemoryTextIndex | None = None,
        trace_store: TraceStore | None = None,
        provider_config: ProviderConfig | None = None,
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
        self.observation_service = observation_service
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
        self.trace_store = trace_store
        self.keyframe_root = Path(keyframe_root)
        self.semantic_store = semantic_store or SessionVisualSemanticStore(
            root=self.keyframe_root / "semantic-store",
            session_id=session_id,
            observer=embedding_coordinator.observer,
        )
        self._owns_semantic_store = semantic_store is None
        self._resource_release = resource_release
        self._resources_released = False
        resolved_provider_config = provider_config or ProviderConfig()
        self.close_wait_seconds = close_wait_seconds
        self.clock_ns = clock_ns
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))
        self.video_id: str | None = None
        self.closed = False
        self._observation_tasks: dict[int, asyncio.Task[None]] = {}
        self._observation_items: dict[int, _ObservationItem] = {}
        self._reserved_sequences: set[int] = set()
        self._owned_paths: set[Path] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._first_terminal_snapshot = asyncio.Event()
        self._snapshot_updated = asyncio.Event()
        self._enqueue_lock = asyncio.Lock()
        self._promotion_tasks: set[asyncio.Task[Any]] = set()
        self._pinned_sequences: dict[int, int] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._semantic_pending_count = 0
        self._semantic_in_flight = False
        self.semantic_pipeline = SemanticFramePipeline(
            coordinator=embedding_coordinator,
            selector=SemanticKeyframeSelector(
                SemanticKeyframeConfig(
                    min_interval_seconds=(
                        resolved_provider_config.keyframe_min_interval_seconds
                    ),
                    max_interval_seconds=(
                        resolved_provider_config.keyframe_max_interval_seconds
                    ),
                    semantic_threshold=(
                        resolved_provider_config.keyframe_semantic_threshold
                    ),
                )
            ),
            sampler=FixedIntervalSemanticSampler(
                fps=resolved_provider_config.semantic_input_fps
            ),
            retention_root=self.keyframe_root / "semantic-input",
            on_selected=self._enqueue_semantic_selection,
            on_state_change=self._update_semantic_pipeline_state,
            observer=embedding_coordinator.observer,
        )

    async def submit(self, frame: VideoFrame) -> FrameProcessingResult:
        """Admit a frame for background semantic processing without waiting."""

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
            sampling_rate=self.semantic_pipeline.sampler.fps,
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
    ) -> WindowPromotionResult:
        """Enqueue one frozen frame window without adaptive latest-wins selection."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        selected = tuple(frames)
        if not selected:
            raise ValueError("realtime visual target window must not be empty")
        _validate_frame_window(selected)
        task = asyncio.create_task(self._promote_window(selected))
        self._promotion_tasks.add(task)
        task.add_done_callback(self._settle_owned_retention)
        return await asyncio.shield(task)

    async def _promote_window(
        self,
        frames: tuple[VideoFrame, ...],
    ) -> WindowPromotionResult:
        target = frames[-1]
        ordered = (target, *frames[:-1])
        outcomes: list[bool] = []
        for frame in ordered:
            self._accept_video_id(frame)
            outcomes.append(
                await self._enqueue(
                    frame,
                    enqueued_ns=self.clock_ns(),
                    keyframe_selection_latency_ms=0,
                )
            )
        return WindowPromotionResult(
            enqueued_sequences=tuple(
                frame.sequence
                for frame, enqueued in zip(ordered, outcomes, strict=True)
                if enqueued
            ),
            reused_sequences=tuple(
                frame.sequence
                for frame, enqueued in zip(ordered, outcomes, strict=True)
                if not enqueued
            ),
        )

    def pin_sequence(self, sequence: int) -> None:
        """Keep one chat target represented until its observation settles."""

        if sequence >= 0:
            self._pinned_sequences[sequence] = self._pinned_sequences.get(sequence, 0) + 1

    def release_sequence(self, sequence: int) -> None:
        """Release a previously protected chat-arrival frame."""

        count = self._pinned_sequences.get(sequence, 0)
        if count <= 1:
            self._pinned_sequences.pop(sequence, None)
        else:
            self._pinned_sequences[sequence] = count - 1

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

    async def _enqueue_semantic_selection(
        self,
        frame: VideoFrame,
        event: EmbeddingEvent | None,
        reason: str,
    ) -> None:
        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        enqueued_ns = self.clock_ns()
        if self.visual_reminder_registry is not None:
            await self.visual_reminder_registry.publish_image_event(
                self.user_id,
                self.session_id,
                event,
            )
        await self._enqueue(
            frame,
            enqueued_ns=enqueued_ns,
            keyframe_selection_latency_ms=_keyframe_selection_latency_ms(
                frame,
                selected_ns=enqueued_ns,
            ),
            already_retained=True,
        )

    async def _enqueue(
        self,
        frame: VideoFrame,
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
        already_retained: bool = False,
    ) -> bool:
        async with self._enqueue_lock:
            return await self._enqueue_serialized(
                frame,
                enqueued_ns=enqueued_ns,
                keyframe_selection_latency_ms=keyframe_selection_latency_ms,
                already_retained=already_retained,
            )

    async def _enqueue_serialized(
        self,
        frame: VideoFrame,
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
        already_retained: bool,
    ) -> bool:
        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        if self._sequence_is_represented(frame.sequence):
            if already_retained:
                self._delete_transferred_duplicate(frame)
            return False
        sequence = frame.sequence
        retained: SemanticKeyframeRecord | None = None
        self._reserved_sequences.add(sequence)
        try:
            retained = (
                SemanticKeyframeRecord(
                    frame_id=frame.frame_id,
                    uri=frame.uri,
                    sequence=sequence,
                    timestamp_ms=frame.timestamp_ms,
                )
                if already_retained
                else await asyncio.to_thread(self._retain_keyframe, frame)
            )
            self._owned_paths.add(Path(retained.uri))
            if self.closed:
                self._delete_record(retained)
                raise RuntimeError("realtime video observer is closed")
            if self.semantic_store.has_exact_sequence(
                frame.video_id,
                sequence=sequence,
            ):
                self._delete_record(retained)
                return False
            snapshot = self.memory_store.snapshot(frame.video_id)
            if snapshot is None or snapshot.last_success_sequence is None:
                self._first_terminal_snapshot.clear()
            video_ingress_ns = _frame_timestamp_ns(frame, "video_ingress_ns")
            item = _ObservationItem(
                record=retained,
                enqueued_ns=enqueued_ns,
                video_ingress_ns=(
                    video_ingress_ns if video_ingress_ns is not None else enqueued_ns
                ),
                h264_decode_latency_ms=_frame_latency_ms(
                    frame,
                    "h264_decode_latency_ms",
                ),
                keyframe_selection_latency_ms=keyframe_selection_latency_ms,
            )
            task = asyncio.create_task(self._run_observation(item))
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
        except BaseException:
            if retained is not None and sequence not in self._observation_tasks:
                self._delete_record(retained)
            raise
        finally:
            self._reserved_sequences.discard(sequence)

    async def wait_idle(self) -> None:
        """Wait until semantic embedding, VLM, and reminder delivery are idle."""

        await self.semantic_pipeline.wait_idle()
        while self._observation_tasks:
            tasks = tuple(self._observation_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._idle.wait()

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
            await asyncio.to_thread(self.observation_service.close)
            if self.video_id is not None:
                self.memory_store.remove_video(self.video_id)
            if self._owns_semantic_store:
                self.semantic_store.close()
            active_paths = {
                Path(item.record.uri) for item in self._observation_items.values()
            }
            for path in self._owned_paths - active_paths:
                path.unlink(missing_ok=True)
                self._owned_paths.discard(path)
            _remove_empty_tree(self.keyframe_root)
            self._idle.set()
            self._snapshot_updated.set()
        finally:
            self._release_external_resources()

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
            trace_context = self._trace_context(item.record)
            outcome = await asyncio.to_thread(
                self.observation_service.observe,
                RealtimeVisualObservationRequest(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    video_id=video_id,
                    frame_ref=item.record.uri,
                    frame_sequence=item.record.sequence,
                    frame_timestamp_ms=item.record.timestamp_ms,
                ),
                trace_context=trace_context,
            )
            observation_finished_ns = self.clock_ns()
            self._record_observation_summary(
                item.record,
                trace_context=trace_context,
                succeeded=outcome.succeeded,
                error=outcome.error,
            )
            if self.closed:
                self._delete_record(item)
                return
            observation = outcome.result if outcome.succeeded else None
            if observation is not None:
                trace_link = outcome.trace_link
                publish_outcome = await asyncio.to_thread(
                    self._publish_visual_semantic_record,
                    video_id,
                    item.record,
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
                    self._delete_record(record)
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
                self._delete_record(item)
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
            self._delete_record(item)
        finally:
            self._first_terminal_snapshot.set()
            self._snapshot_updated.set()

    def _settle_observation_task(
        self,
        sequence: int,
        task: asyncio.Task[None],
    ) -> None:
        if self._observation_tasks.get(sequence) is task:
            self._observation_tasks.pop(sequence, None)
            self._observation_items.pop(sequence, None)
        if not task.cancelled():
            task.exception()
        self._update_pending_state()
        if not self._observation_tasks:
            self._idle.set()

    def _publish_visual_semantic_record(
        self,
        video_id: str,
        frame: SemanticKeyframeRecord,
        result: VideoUnderstandingResult,
        trace_link: VisionInferenceTraceLink | None,
    ) -> _VisualSemanticPublishOutcome:
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

    def _trace_context(
        self,
        item: SemanticKeyframeRecord,
    ) -> _BackgroundVisionTraceContext | None:
        if self.trace_store is None:
            return None
        observation_id = uuid4().hex
        return _BackgroundVisionTraceContext(
            trace_store=self.trace_store,
            trace_id=f"visual-trace-{observation_id}",
            run_id=f"visual-observation-{item.sequence}-{observation_id}",
            user_id=self.user_id,
            session_id=self.session_id,
        )

    def _record_observation_summary(
        self,
        item: SemanticKeyframeRecord,
        *,
        trace_context: _BackgroundVisionTraceContext | None,
        succeeded: bool,
        error: dict[str, Any] | None,
    ) -> None:
        if trace_context is None:
            return
        try:
            append_observability_event(
                trace_context.trace_store,
                trace_id=trace_context.trace_id,
                run_id=trace_context.run_id,
                user_id=self.user_id,
                session_id=self.session_id,
                canonical_event="vision.observation.summary",
                node_name="realtime_video_observer",
                status="completed" if succeeded else "failed",
                attributes={
                    "trace_kind": "vision_observation",
                    "source": "realtime_video_observer",
                    "media_kind": "live_view",
                    "frame_sequence": item.sequence,
                },
                output_summary={
                    "status": "succeeded" if succeeded else "failed",
                },
                error=(
                    None
                    if error is None
                    else {
                        "code": error.get(
                            "code", "video_observation_failed"
                        ),
                        "message": "VLM observation failed.",
                    }
                ),
            )
        except Exception:
            pass

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
        destination = directory / f"frame-{frame.sequence:06d}.jpg"
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

    def _delete_record(self, record: SemanticKeyframeRecord | _ObservationItem) -> None:
        if isinstance(record, _ObservationItem):
            record = record.record
        path = Path(record.uri)
        path.unlink(missing_ok=True)
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
            Path(item.record.uri) for item in self._observation_items.values()
        }
        if path not in active_paths:
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


def _keyframe_selection_latency_ms(
    frame: VideoFrame,
    *,
    selected_ns: int,
) -> int:
    ingress_ns = _frame_timestamp_ns(frame, "video_ingress_ns")
    if ingress_ns is None:
        return 0
    ingress_to_selection_ms = _elapsed_ms(ingress_ns, selected_ns)
    decode_latency_ms = _frame_latency_ms(frame, "h264_decode_latency_ms") or 0
    return max(0, ingress_to_selection_ms - decode_latency_ms)


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
