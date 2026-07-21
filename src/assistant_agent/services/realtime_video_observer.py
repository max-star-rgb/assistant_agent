"""Governed bounded background observation for realtime video keyframes."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns, time
from typing import Any

from pydantic import ValidationError

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.perception import VideoUnderstandingResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.schemas.tool_ids import IMAGE_UNDERSTANDING_TOOL_NAME
from assistant_agent.services.video_context import VideoFrame
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.video_ai.keyframe.collector import AdaptiveKeyframeCollector
from assistant_agent.video_ai.keyframe.selector import KeyframeSelectorConfig
from assistant_agent.video_ai.sampling.adaptive_sampler import AdaptiveSamplerConfig
from assistant_agent.video_ai.types import (
    FrameProcessingResult,
    KeyframeChangeMetrics,
    VideoFrame as AIVideoFrame,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KEYFRAME_ROOT = REPO_ROOT / ".data" / "agent_service_video_keyframes"
DEFAULT_CLOSE_WAIT_SECONDS = 1.0
REALTIME_PREVIOUS_SUMMARY_MAX_CHARS = 2_000
REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class _QueuedObservation:
    record: SemanticKeyframeRecord
    enqueued_ns: int
    h264_decode_latency_ms: int | None
    keyframe_selection_latency_ms: int


class RealtimeVideoObserver:
    """Select and analyze keyframes without blocking the media receive loop."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        registry: ToolRegistry,
        memory_store: RealtimeVideoMemoryStore,
        keyframe_root: Path | str = DEFAULT_KEYFRAME_ROOT,
        collector: AdaptiveKeyframeCollector | None = None,
        validator: ActionValidator | None = None,
        close_wait_seconds: float = DEFAULT_CLOSE_WAIT_SECONDS,
        clock_ns: Callable[[], int] = perf_counter_ns,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if close_wait_seconds <= 0:
            raise ValueError("close_wait_seconds must be positive")
        self.user_id = user_id
        self.session_id = session_id
        self.registry = registry
        self.memory_store = memory_store
        self.keyframe_root = Path(keyframe_root)
        self.collector = collector or AdaptiveKeyframeCollector(
            sampler_config=AdaptiveSamplerConfig(immediate_change_threshold=0.35),
            keyframe_config=KeyframeSelectorConfig(
                min_interval_seconds=0.0,
                max_interval_seconds=REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS,
            )
        )
        self.validator = validator or ActionValidator()
        self.close_wait_seconds = close_wait_seconds
        self.clock_ns = clock_ns
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))
        self.video_id: str | None = None
        self.closed = False
        self._queue: asyncio.Queue[_QueuedObservation] = asyncio.Queue(maxsize=1)
        self._worker: asyncio.Task[None] | None = None
        self._execution_task: asyncio.Task[ToolResult] | None = None
        self._inflight_item: _QueuedObservation | None = None
        self._pending_item: _QueuedObservation | None = None
        self._owned_paths: set[Path] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._first_terminal_snapshot = asyncio.Event()
        self._snapshot_updated = asyncio.Event()
        self._enqueue_lock = asyncio.Lock()
        self._promotion_tasks: set[asyncio.Task[FrameProcessingResult]] = set()
        self._close_task: asyncio.Task[None] | None = None

    async def submit(self, frame: VideoFrame) -> FrameProcessingResult:
        """Run local selection and enqueue a selected frame for background analysis."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        return await self._run_owned_retention(self._submit(frame))

    async def _submit(self, frame: VideoFrame) -> FrameProcessingResult:
        self._accept_video_id(frame)

        selection_started_ns = self.clock_ns()
        ai_frame = _to_ai_frame(frame)
        collection = await asyncio.to_thread(self.collector.collect, ai_frame)
        if collection.selected_frame is None:
            return collection.processing

        selection_finished_ns = self.clock_ns()
        await self._enqueue(
            frame,
            enqueued_ns=selection_finished_ns,
            keyframe_selection_latency_ms=_elapsed_ms(
                selection_started_ns,
                selection_finished_ns,
            ),
        )
        return collection.processing

    async def promote(self, frame: VideoFrame) -> FrameProcessingResult:
        """Enqueue a decoded frame without adaptive selection."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        return await self._run_owned_retention(self._promote(frame))

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
        represented = self._represented_sequence()
        if represented is None or represented < frame.sequence:
            enqueued_ns = self.clock_ns()
            enqueued = await self._enqueue(
                frame,
                enqueued_ns=enqueued_ns,
                keyframe_selection_latency_ms=_elapsed_ms(started_ns, enqueued_ns),
            )
            reason = (
                "promoted_for_realtime_visual_freshness"
                if enqueued
                else "realtime_visual_sequence_already_represented"
            )
        else:
            reason = "realtime_visual_sequence_already_represented"
        return FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=_to_ai_frame(frame).timestamp_seconds,
            sampled=True,
            sampling_rate=0.0,
            metrics=KeyframeChangeMetrics(),
            keyframe_selected=True,
            qwen_called=False,
            latency_ms=_elapsed_ms(started_ns, self.clock_ns()),
            decision_reason=reason,
        )

    async def _enqueue(
        self,
        frame: VideoFrame,
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
    ) -> bool:
        async with self._enqueue_lock:
            return await self._enqueue_serialized(
                frame,
                enqueued_ns=enqueued_ns,
                keyframe_selection_latency_ms=keyframe_selection_latency_ms,
            )

    async def _enqueue_serialized(
        self,
        frame: VideoFrame,
        *,
        enqueued_ns: int,
        keyframe_selection_latency_ms: int,
    ) -> bool:
        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        represented = self._represented_sequence()
        if represented is not None and represented >= frame.sequence:
            return False
        retained = await asyncio.to_thread(self._retain_keyframe, frame)
        self._owned_paths.add(Path(retained.uri))
        if self.closed:
            self._delete_record(retained)
            raise RuntimeError("realtime video observer is closed")
        represented = self._represented_sequence()
        if represented is not None and represented >= frame.sequence:
            self._delete_record(retained)
            return False
        snapshot = self.memory_store.snapshot(frame.video_id)
        if snapshot is None or snapshot.last_success_sequence is None:
            self._first_terminal_snapshot.clear()
        queued = _QueuedObservation(
            record=retained,
            enqueued_ns=enqueued_ns,
            h264_decode_latency_ms=_frame_latency_ms(frame, "h264_decode_latency_ms"),
            keyframe_selection_latency_ms=keyframe_selection_latency_ms,
        )
        if self._queue.full():
            replaced = self._queue.get_nowait()
            self._queue.task_done()
            self._delete_record(replaced)
        self._queue.put_nowait(queued)
        self._pending_item = queued
        self._idle.clear()
        self._ensure_worker()
        self._update_pending_state()
        return True

    async def wait_idle(self) -> None:
        """Wait until the bounded queue and current observation are finished."""

        await self._queue.join()
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
        self._drop_pending()
        await self.wait_for_promotions()

        execution = self._execution_task
        if execution is not None and not execution.done():
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=self.close_wait_seconds)
            except TimeoutError:
                inflight = self._inflight_item
                if inflight is not None:
                    path = Path(inflight.record.uri)
                    self._delete_record(inflight)
                    execution.add_done_callback(lambda _task, owned=path: self._delete_late_path(owned))

        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._close_video_adapter()
        if self.video_id is not None:
            self.memory_store.remove_video(self.video_id)
        for path in list(self._owned_paths):
            path.unlink(missing_ok=True)
        self._owned_paths.clear()
        _remove_empty_tree(self.keyframe_root)
        self._idle.set()
        self._snapshot_updated.set()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while not self.closed:
            item = await self._queue.get()
            if self._pending_item is item:
                self._pending_item = None
            self._inflight_item = item
            dequeued_ns = self.clock_ns()
            self._update_pending_state()
            try:
                observation_started_ns = self.clock_ns()
                self._execution_task = asyncio.create_task(
                    asyncio.to_thread(self._execute_observation, item.record)
                )
                result = await asyncio.shield(self._execution_task)
                observation_finished_ns = self.clock_ns()
                if self.closed:
                    self._delete_record(item)
                    continue
                observation = _snapshot_publishable_observation(result)
                if observation is not None:
                    diagnostics = self._observation_diagnostics(
                        item=item,
                        dequeued_ns=dequeued_ns,
                        observation_started_ns=observation_started_ns,
                        observation_finished_ns=observation_finished_ns,
                        succeeded=True,
                    ).model_copy(
                        update={"published_at_ms": self.wall_clock_ms()}
                    )
                    evicted = self.memory_store.record_success(
                        item_video_id(item.record, self.video_id),
                        item.record,
                        observation,
                        diagnostics=diagnostics,
                    )
                    for record in evicted:
                        self._delete_record(record)
                else:
                    self.memory_store.record_failure(
                        item_video_id(item.record, self.video_id),
                        item.record,
                        _result_error(result),
                        diagnostics=self._observation_diagnostics(
                            item=item,
                            dequeued_ns=dequeued_ns,
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
                    self.memory_store.record_failure(
                        item_video_id(item.record, self.video_id),
                        item.record,
                        {
                            "code": "video_observation_failed",
                            "message": sanitize_error_message(exc),
                            "recoverable": True,
                        },
                    )
                self._delete_record(item)
            finally:
                self._execution_task = None
                self._inflight_item = None
                self._queue.task_done()
                self._update_pending_state()
                self._first_terminal_snapshot.set()
                self._snapshot_updated.set()
                if self._queue.empty():
                    self._idle.set()

    def _execute_observation(self, item: SemanticKeyframeRecord) -> ToolResult:
        video_id = item_video_id(item, self.video_id)
        request = UserRequest(
            user_id=self.user_id,
            session_id=self.session_id,
            text="Update rolling realtime video state from a selected keyframe.",
            video_ids=[video_id],
            metadata={"source": "realtime_video_observer"},
        )
        state = AgentState.from_request(request)
        snapshot = self.memory_store.snapshot(video_id)
        tool_input: dict[str, Any] = {
            "video_ref": video_id,
            "frame_refs": [item.uri],
            "user_query": "更新当前场景、物体、人物、动作和重要变化。",
            "metadata": {
                "frame_id": item.frame_id,
                "frame_sequence": item.sequence,
                "frame_timestamp_ms": item.timestamp_ms,
            },
            "memory_context": (
                snapshot.current_state[:REALTIME_PREVIOUS_SUMMARY_MAX_CHARS]
                if snapshot is not None and snapshot.current_state
                else None
            ),
        }
        decision = AssistantDecision(
            type="tool_call",
            tool_name=IMAGE_UNDERSTANDING_TOOL_NAME,
            tool_input=tool_input,
            reason="Observe a selected realtime video keyframe.",
        )
        validation = self.validator.validate(
            decision=decision,
            registry=self.registry,
            request=request,
            state=state,
        )
        if not validation.accepted:
            return ToolResult(
                tool_name=IMAGE_UNDERSTANDING_TOOL_NAME,
                success=False,
                error=f"{validation.code}: {validation.message}",
            )
        executor = ToolExecutor(
            registry=self.registry,
            context_metadata={"realtime_video_observation": True},
        )
        return executor.run_tool(
            state,
            f"video-observation-{item.sequence}",
            IMAGE_UNDERSTANDING_TOOL_NAME,
            tool_input,
            node_name="realtime_video_observer",
        )

    async def _close_video_adapter(self) -> None:
        try:
            tool = self.registry.get(IMAGE_UNDERSTANDING_TOOL_NAME)
        except KeyError:
            return
        adapter = getattr(tool, "video_adapter", None)
        close = getattr(adapter, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    def _observation_diagnostics(
        self,
        *,
        item: _QueuedObservation,
        dequeued_ns: int,
        observation_started_ns: int,
        observation_finished_ns: int,
        succeeded: bool,
    ) -> RealtimeVideoObservationDiagnostics:
        provider = self._provider_diagnostics()
        return RealtimeVideoObservationDiagnostics(
            h264_decode_latency_ms=item.h264_decode_latency_ms,
            keyframe_selection_latency_ms=item.keyframe_selection_latency_ms,
            queue_wait_latency_ms=_elapsed_ms(item.enqueued_ns, dequeued_ns),
            observation_latency_ms=_elapsed_ms(
                observation_started_ns,
                observation_finished_ns,
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
        )

    def _provider_diagnostics(self) -> dict[str, Any]:
        try:
            tool = self.registry.get(IMAGE_UNDERSTANDING_TOOL_NAME)
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

    def _drop_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if self._pending_item is item:
                self._pending_item = None
            self._delete_record(item)

    def _delete_record(self, record: SemanticKeyframeRecord | _QueuedObservation) -> None:
        if isinstance(record, _QueuedObservation):
            record = record.record
        path = Path(record.uri)
        path.unlink(missing_ok=True)
        self._owned_paths.discard(path)

    def _delete_late_path(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        _remove_empty_tree(self.keyframe_root)

    def _update_pending_state(self) -> None:
        if self.video_id is None or self.closed:
            return
        self.memory_store.mark_pending(
            self.video_id,
            pending_count=self._queue.qsize(),
            in_flight=self._inflight_item is not None,
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
            self._inflight_item.record.sequence if self._inflight_item is not None else None,
            self._pending_item.record.sequence if self._pending_item is not None else None,
        ]
        represented = [sequence for sequence in sequences if sequence is not None]
        return max(represented) if represented else None


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


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, int((end_ns - start_ns) / 1_000_000))


def item_video_id(item: SemanticKeyframeRecord, video_id: str | None) -> str:
    _ = item
    if video_id is None:
        raise RuntimeError("video id is not initialized")
    return video_id


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
