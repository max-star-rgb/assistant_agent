"""Governed bounded background observation for realtime video keyframes."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

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
    SemanticKeyframeRecord,
)
from assistant_agent.services.video_context import VideoFrame
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.video_ai.keyframe.collector import AdaptiveKeyframeCollector
from assistant_agent.video_ai.types import FrameProcessingResult, VideoFrame as AIVideoFrame


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KEYFRAME_ROOT = REPO_ROOT / ".data" / "agent_service_video_keyframes"
DEFAULT_CLOSE_WAIT_SECONDS = 1.0


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
    ) -> None:
        if close_wait_seconds <= 0:
            raise ValueError("close_wait_seconds must be positive")
        self.user_id = user_id
        self.session_id = session_id
        self.registry = registry
        self.memory_store = memory_store
        self.keyframe_root = Path(keyframe_root)
        self.collector = collector or AdaptiveKeyframeCollector()
        self.validator = validator or ActionValidator()
        self.close_wait_seconds = close_wait_seconds
        self.video_id: str | None = None
        self.closed = False
        self._queue: asyncio.Queue[SemanticKeyframeRecord] = asyncio.Queue(maxsize=1)
        self._worker: asyncio.Task[None] | None = None
        self._execution_task: asyncio.Task[ToolResult] | None = None
        self._inflight_item: SemanticKeyframeRecord | None = None
        self._owned_paths: set[Path] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    async def submit(self, frame: VideoFrame) -> FrameProcessingResult:
        """Run local selection and enqueue a selected frame for background analysis."""

        if self.closed:
            raise RuntimeError("realtime video observer is closed")
        if self.video_id is None:
            self.video_id = frame.video_id
        elif self.video_id != frame.video_id:
            raise ValueError("realtime video observer accepts one video id")

        ai_frame = _to_ai_frame(frame)
        collection = await asyncio.to_thread(self.collector.collect, ai_frame)
        if collection.selected_frame is None:
            return collection.processing

        retained = await asyncio.to_thread(self._retain_keyframe, frame)
        if self._queue.full():
            replaced = self._queue.get_nowait()
            self._queue.task_done()
            self._delete_record(replaced)
        self._queue.put_nowait(retained)
        self._idle.clear()
        self._ensure_worker()
        self._update_pending_state()
        return collection.processing

    async def wait_idle(self) -> None:
        """Wait until the bounded queue and current observation are finished."""

        await self._queue.join()
        await self._idle.wait()

    async def close(self) -> None:
        """Stop work, reject late results, and remove owned semantic artifacts."""

        if self.closed:
            return
        self.closed = True
        self._drop_pending()

        execution = self._execution_task
        if execution is not None and not execution.done():
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=self.close_wait_seconds)
            except TimeoutError:
                inflight = self._inflight_item
                if inflight is not None:
                    path = Path(inflight.uri)
                    self._owned_paths.discard(path)
                    execution.add_done_callback(lambda _task, owned=path: self._delete_late_path(owned))

        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self.video_id is not None:
            self.memory_store.remove_video(self.video_id)
        for path in list(self._owned_paths):
            path.unlink(missing_ok=True)
        self._owned_paths.clear()
        _remove_empty_tree(self.keyframe_root)
        self._idle.set()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while not self.closed:
            item = await self._queue.get()
            self._inflight_item = item
            self._update_pending_state()
            try:
                self._execution_task = asyncio.create_task(asyncio.to_thread(self._execute_observation, item))
                result = await asyncio.shield(self._execution_task)
                if self.closed:
                    self._delete_record(item)
                    continue
                if result.success and result.data:
                    observation = VideoUnderstandingResult.model_validate(result.data)
                    evicted = self.memory_store.record_success(item_video_id(item, self.video_id), item, observation)
                    for record in evicted:
                        self._delete_record(record)
                else:
                    self.memory_store.record_failure(
                        item_video_id(item, self.video_id),
                        item,
                        _result_error(result),
                    )
                    self._delete_record(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background boundary.
                if not self.closed:
                    self.memory_store.record_failure(
                        item_video_id(item, self.video_id),
                        item,
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
        history_refs = [record.uri for record in snapshot.keyframes[-2:]] if snapshot is not None else []
        tool_input: dict[str, Any] = {
            "video_ref": video_id,
            "frame_refs": [*history_refs, item.uri],
            "user_query": "更新当前场景、物体、人物、动作和重要变化。",
            "metadata": {
                "frame_id": item.frame_id,
                "frame_sequence": item.sequence,
                "frame_timestamp_ms": item.timestamp_ms,
            },
        }
        decision = AssistantDecision(
            type="tool_call",
            tool_name="video_understanding",
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
                tool_name="video_understanding",
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
            "video_understanding",
            tool_input,
            node_name="realtime_video_observer",
        )

    def _retain_keyframe(self, frame: VideoFrame) -> SemanticKeyframeRecord:
        suffix = _safe_name(frame.video_id.removeprefix("agent-service-video-"))
        directory = self.keyframe_root / suffix
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"frame-{frame.sequence:06d}.jpg"
        shutil.copy2(frame.uri, destination)
        destination = destination.resolve()
        self._owned_paths.add(destination)
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
            self._delete_record(item)

    def _delete_record(self, record: SemanticKeyframeRecord) -> None:
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
    return {
        "code": "video_observation_failed",
        "message": sanitize_error_message(result.error or "Video observation failed."),
        "recoverable": True,
    }


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
