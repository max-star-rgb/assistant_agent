"""Governed bounded background observation for realtime video keyframes."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns, time
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
)
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent, TextObservation
from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.observability import VisionInferenceTraceLink
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.tools.ids import REALTIME_VIDEO_OBSERVE_TOOL_NAME
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.semantic_pipeline import (
    FixedIntervalSemanticSampler,
    SemanticFramePipeline,
)
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.tools.registry import ToolRegistry
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
    text_embedding_latency_ms: int
    semantic_store_write_latency_ms: int


ObservationRegistryFactory = Callable[[], ToolRegistry]


class RealtimeVideoObserver:
    """Select and analyze keyframes without blocking the media receive loop."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        registry: ToolRegistry | None,
        memory_store: RealtimeVideoMemoryStore,
        semantic_store: SessionVisualSemanticStore | None = None,
        embedding_coordinator: SessionEmbeddingCoordinator,
        observation_registry_factory: ObservationRegistryFactory | None = None,
        visual_reminder_registry: VisualReminderRegistry | None = None,
        trace_store: TraceStore | None = None,
        provider_config: ProviderConfig | None = None,
        keyframe_root: Path | str = DEFAULT_KEYFRAME_ROOT,
        validator: ActionValidator | None = None,
        close_wait_seconds: float = DEFAULT_CLOSE_WAIT_SECONDS,
        resource_release: Callable[[], None] | None = None,
        clock_ns: Callable[[], int] = perf_counter_ns,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if close_wait_seconds <= 0:
            raise ValueError("close_wait_seconds must be positive")
        if registry is None and observation_registry_factory is None:
            raise ValueError("realtime video observer requires a registry or factory")
        self.user_id = user_id
        self.session_id = session_id
        self.registry = registry
        self.observation_registry_factory = observation_registry_factory
        self.memory_store = memory_store
        self.embedding_coordinator = embedding_coordinator
        self.visual_reminder_registry = visual_reminder_registry
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
        self.validator = validator or ActionValidator()
        self.close_wait_seconds = close_wait_seconds
        self.clock_ns = clock_ns
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))
        self.video_id: str | None = None
        self.closed = False
        self._observation_tasks: dict[int, asyncio.Task[None]] = {}
        self._observation_items: dict[int, _ObservationItem] = {}
        self._owned_paths: set[Path] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._first_terminal_snapshot = asyncio.Event()
        self._snapshot_updated = asyncio.Event()
        self._enqueue_lock = asyncio.Lock()
        self._promotion_tasks: set[asyncio.Task[FrameProcessingResult]] = set()
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
        task: asyncio.Task[FrameProcessingResult],
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
            return False
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
        if self.closed:
            self._delete_record(retained)
            raise RuntimeError("realtime video observer is closed")
        if self._sequence_is_represented(frame.sequence):
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
            h264_decode_latency_ms=_frame_latency_ms(frame, "h264_decode_latency_ms"),
            keyframe_selection_latency_ms=keyframe_selection_latency_ms,
        )
        sequence = retained.sequence
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
            if self.observation_registry_factory is None and self.registry is not None:
                await asyncio.to_thread(self._close_registry_adapter, self.registry)
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
        registry: ToolRegistry | None = None
        started_ns = self.clock_ns()
        observation_started_ns = started_ns
        self._update_pending_state()
        try:
            registry = (
                self.observation_registry_factory()
                if self.observation_registry_factory is not None
                else self.registry
            )
            if registry is None:
                raise RuntimeError("realtime observation registry is unavailable")
            result = await asyncio.to_thread(
                self._execute_observation,
                item.record,
                registry,
            )
            observation_finished_ns = self.clock_ns()
            if self.closed:
                self._delete_record(item)
                return
            observation = _snapshot_publishable_observation(result)
            if observation is not None:
                video_id = item_video_id(item.record, self.video_id)
                trace_link = _vision_trace_link(result)
                publish_outcome = await asyncio.to_thread(
                    self._publish_visual_semantic_record,
                    video_id,
                    item.record,
                    observation,
                    trace_link,
                )
                semantic_published_ns = self.clock_ns()
                diagnostics = self._observation_diagnostics(
                    registry=registry,
                    item=item,
                    dequeued_ns=started_ns,
                    observation_started_ns=observation_started_ns,
                    observation_finished_ns=observation_finished_ns,
                    succeeded=True,
                    semantic_published_ns=semantic_published_ns,
                ).model_copy(
                    update={
                        "published_at_ms": self.wall_clock_ms(),
                        "text_embedding_latency_ms": (
                            publish_outcome.text_embedding_latency_ms
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
                video_id = item_video_id(item.record, self.video_id)
                error = _result_error(result)
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
                        registry=registry,
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
            if self.observation_registry_factory is not None and registry is not None:
                await asyncio.to_thread(self._close_registry_adapter, registry)
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
        embedding_started_ns = self.clock_ns()
        text_outcome = self.embedding_coordinator.embed_text(
            TextObservation(
                session_id=self.session_id,
                observation_id=f"visual-record:{video_id}:{frame.sequence}",
                text=_build_visual_search_text(result),
                source="visual_semantic_record",
                occurred_at_ms=frame.timestamp_ms,
            ),
            priority="background",
        )
        embedding_finished_ns = self.clock_ns()
        text_event = text_outcome if isinstance(text_outcome, EmbeddingEvent) else None
        evidence_bytes = Path(frame.uri).stat().st_size
        record = VisualSemanticRecord(
            record_id=f"visual-{uuid4().hex}",
            session_id=self.session_id,
            video_id=video_id,
            frame_sequence=frame.sequence,
            captured_at_ms=frame.timestamp_ms,
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
            search_embedding=(list(text_event.vector) if text_event else None),
            embedding_space_id=(
                text_event.embedding_space_id if text_event else None
            ),
            index_status=("ready" if text_event else "unavailable"),
            evidence_ref=frame.uri,
            evidence_bytes=evidence_bytes,
            created_at_ms=self.wall_clock_ms(),
        )
        store_started_ns = self.clock_ns()
        self.semantic_store.record_success(record)
        store_finished_ns = self.clock_ns()
        if text_event is None:
            emit_visual_semantic_observation(
                self.embedding_coordinator.observer,
                "visual_semantic.index_failed",
                session_id=self.session_id,
                sequence=frame.sequence,
                status="unavailable",
            )
        return _VisualSemanticPublishOutcome(
            visual_record_id=record.record_id,
            text_embedding_latency_ms=_elapsed_ms(
                embedding_started_ns,
                embedding_finished_ns,
            ),
            semantic_store_write_latency_ms=_elapsed_ms(
                store_started_ns,
                store_finished_ns,
            ),
        )

    def _execute_observation(
        self,
        item: SemanticKeyframeRecord,
        registry: ToolRegistry,
    ) -> ToolResult:
        video_id = item_video_id(item, self.video_id)
        user_query = "简短描述当前单帧中直接可见的内容。"
        request = UserRequest(
            user_id=self.user_id,
            session_id=self.session_id,
            text="Describe one selected realtime video frame.",
            video_ids=[video_id],
            metadata={"source": "realtime_video_observer"},
        )
        state = AgentState.from_request(request)
        memory_context = None
        tool_input: dict[str, Any] = {
            "video_ref": video_id,
            "frame_refs": [item.uri],
            "user_query": user_query,
            "metadata": {
                "frame_id": item.frame_id,
                "frame_sequence": item.sequence,
                "frame_timestamp_ms": item.timestamp_ms,
                "visual_context_compaction": {
                    "status": "disabled_single_frame_text",
                    "compacted": False,
                },
            },
            "memory_context": memory_context,
        }
        decision = AssistantDecision(
            type="tool_call",
            tool_name=REALTIME_VIDEO_OBSERVE_TOOL_NAME,
            tool_input={},
            reason="Observe a selected realtime video keyframe.",
        )
        validation = self.validator.validate(
            decision=decision,
            registry=registry,
            request=request,
            state=state,
        )
        if not validation.accepted:
            return ToolResult(
                tool_name=REALTIME_VIDEO_OBSERVE_TOOL_NAME,
                success=False,
                error=f"{validation.code}: {validation.message}",
            )
        executor = ToolExecutor(
            registry=registry,
            context_metadata={"realtime_video_observation": True},
        )
        result = executor.run_tool(
            state,
            f"video-observation-{item.sequence}",
            REALTIME_VIDEO_OBSERVE_TOOL_NAME,
            {},
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="realtime_video_observer",
            runtime_input=tool_input,
        )
        result_error = _result_error(result) if not result.success else None
        try:
            append_observability_event(
                self.trace_store,
                trace_id=state.trace_id,
                run_id=state.run_id,
                user_id=self.user_id,
                session_id=self.session_id,
                canonical_event="vision.observation.summary",
                node_name="realtime_video_observer",
                status="completed" if result.success else "failed",
                attributes={
                    "trace_kind": "vision_observation",
                    "source": "realtime_video_observer",
                    "media_kind": "live_view",
                    "frame_sequence": item.sequence,
                },
                output_summary={
                    "status": "succeeded" if result.success else "failed",
                },
                error=(
                    None
                    if result_error is None
                    else {
                        "code": result_error.get(
                            "code", "video_observation_failed"
                        ),
                        "message": "VLM observation failed.",
                    }
                ),
            )
        except Exception:
            pass
        return result

    @staticmethod
    def _close_registry_adapter(registry: ToolRegistry) -> None:
        try:
            tool = registry.get(REALTIME_VIDEO_OBSERVE_TOOL_NAME)
        except KeyError:
            return
        adapter = getattr(tool, "video_adapter", None)
        close = getattr(adapter, "close", None)
        if callable(close):
            close()

    def _observation_diagnostics(
        self,
        *,
        registry: ToolRegistry,
        item: _ObservationItem,
        dequeued_ns: int,
        observation_started_ns: int,
        observation_finished_ns: int,
        succeeded: bool,
        semantic_published_ns: int | None = None,
    ) -> RealtimeVideoObservationDiagnostics:
        provider = self._provider_diagnostics(registry)
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

    @staticmethod
    def _provider_diagnostics(registry: ToolRegistry) -> dict[str, Any]:
        try:
            tool = registry.get(REALTIME_VIDEO_OBSERVE_TOOL_NAME)
        except KeyError:
            return {}
        adapter = getattr(tool, "video_adapter", None)
        diagnostics = getattr(adapter, "last_observation_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

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
        if sequence in self._observation_tasks:
            return True
        record = self.semantic_store.at_or_before(
            self.video_id or "",
            sequence=sequence,
        )
        return record is not None and record.frame_sequence == sequence

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


def _vision_trace_link(result: ToolResult) -> VisionInferenceTraceLink | None:
    summary = result.trace_summary
    if not isinstance(summary, dict):
        return None
    values = {
        "trace_id": summary.get("source_vision_trace_id"),
        "run_id": summary.get("source_vision_run_id"),
        "span_id": summary.get("source_vlm_span_id"),
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return VisionInferenceTraceLink.model_validate(values)


def _result_error(result: ToolResult) -> dict[str, Any]:
    contract_errors = result.contract.errors if result.contract is not None else []
    first = contract_errors[0].model_dump(mode="json") if contract_errors else None
    if isinstance(first, dict):
        return {
            "code": str(first.get("code") or "video_observation_failed"),
            "message": sanitize_error_message(first.get("message") or result.error or "Video observation failed."),
            "recoverable": bool(first.get("recoverable", True)),
        }
    data_errors = result.data.get("errors") if isinstance(result.data, dict) else None
    if isinstance(data_errors, list) and data_errors:
        first_data_error = data_errors[0]
        if isinstance(first_data_error, dict):
            return {
                "code": str(first_data_error.get("code") or "video_observation_failed"),
                "message": sanitize_error_message(
                    first_data_error.get("message") or result.error or "Video observation failed."
                ),
                "recoverable": bool(first_data_error.get("recoverable", True)),
            }
    if result.success:
        return {
            "code": "realtime_video_snapshot_not_publishable",
            "message": "Video observation result is not publishable as a realtime video semantic snapshot.",
            "recoverable": True,
        }
    return {
        "code": "video_observation_failed",
        "message": sanitize_error_message(result.error or "Video observation failed."),
        "recoverable": True,
    }


def _snapshot_publishable_observation(result: ToolResult) -> VideoUnderstandingResult | None:
    """Return a VLM result only when it may publish a rolling semantic snapshot."""

    if not result.success or not isinstance(result.data, dict):
        return None
    try:
        observation = VideoUnderstandingResult.model_validate(result.data)
    except ValidationError:
        return None
    if observation.errors:
        return None
    if result.data.get("source") != "background_keyframe_observation":
        return None
    return observation


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
