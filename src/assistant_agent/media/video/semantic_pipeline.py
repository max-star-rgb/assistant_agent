"""Fixed-rate, bounded semantic processing for realtime video frames."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingOutcome,
    ImageObservation,
)
from assistant_agent.media.embedding.observability import (
    EmbeddingObserver,
    emit_semantic_frame_observation,
)
from assistant_agent.media.video.keyframe.selector import (
    SemanticKeyframeDecision,
    SemanticKeyframeSelector,
)
from assistant_agent.media.video.video_context import VideoFrame


SemanticPriority = Literal["background", "interactive"]
SelectedCallback = Callable[
    [VideoFrame, EmbeddingEvent | None, str],
    Awaitable[None],
]
EmbeddedCallback = Callable[[VideoFrame, EmbeddingEvent], Awaitable[None]]
StateCallback = Callable[[int, bool], None]


class ImageEmbeddingCoordinator(Protocol):
    session_id: str

    def embed_image(
        self,
        observation: ImageObservation,
        *,
        priority: SemanticPriority,
    ) -> EmbeddingOutcome: ...


class FixedIntervalSemanticSampler:
    """Admit increasing frame sequences at a fixed monotonic-time interval."""

    def __init__(self, *, fps: float = 5.0) -> None:
        if fps <= 0:
            raise ValueError("semantic input FPS must be positive")
        self.fps = fps
        self.interval_seconds = 1.0 / fps
        self._last_seen_sequence: int | None = None
        self._last_admitted_at: float | None = None

    def admit(self, *, sequence: int, now: float) -> bool:
        if sequence < 0:
            return False
        if self._last_seen_sequence is not None and sequence <= self._last_seen_sequence:
            return False
        self._last_seen_sequence = sequence
        if self._last_admitted_at is not None:
            elapsed = now - self._last_admitted_at
            if elapsed < self.interval_seconds:
                return False
        self._last_admitted_at = now
        return True


@dataclass(frozen=True)
class SemanticAdmission:
    """Immediate admission result returned before embedding or VLM work."""

    admitted: bool
    reason: str
    sequence: int
    replaced_sequence: int | None = None


@dataclass(frozen=True)
class _SemanticFrameJob:
    frame: VideoFrame
    priority: SemanticPriority
    pinned: bool


class SemanticFramePipeline:
    """Run one image embedding with one bounded pending slot."""

    def __init__(
        self,
        *,
        coordinator: ImageEmbeddingCoordinator,
        selector: SemanticKeyframeSelector,
        sampler: FixedIntervalSemanticSampler,
        retention_root: Path | str,
        on_selected: SelectedCallback,
        on_embedded: EmbeddedCallback | None = None,
        on_state_change: StateCallback | None = None,
        observer: EmbeddingObserver | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.coordinator = coordinator
        self.selector = selector
        self.sampler = sampler
        self.retention_root = Path(retention_root)
        self.on_selected = on_selected
        self.on_embedded = on_embedded
        self.on_state_change = on_state_change
        self.observer = observer
        self.clock = clock
        self._pending: _SemanticFrameJob | None = None
        self._inflight: _SemanticFrameJob | None = None
        self._interactive_sequences: set[int] = set()
        self._owned_paths: set[Path] = set()
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    async def submit(self, frame: VideoFrame) -> SemanticAdmission:
        """Admit a background frame without waiting for embedding."""

        if self._closed:
            raise RuntimeError("semantic frame pipeline is closed")
        if not self.sampler.admit(sequence=frame.sequence, now=self.clock()):
            self._emit("semantic_frame.skipped", frame.sequence, "fixed_interval")
            return SemanticAdmission(
                admitted=False,
                reason="fixed_interval",
                sequence=frame.sequence,
            )
        retained = await asyncio.to_thread(self._retain_frame, frame)
        return await self._put(
            _SemanticFrameJob(
                frame=retained,
                priority="background",
                pinned=False,
            )
        )

    async def promote(self, frame: VideoFrame) -> SemanticAdmission:
        """Pin an interactive frame so later background work cannot replace it."""

        async with self._lock:
            if self._closed:
                raise RuntimeError("semantic frame pipeline is closed")
            if (
                self._inflight is not None
                and self._inflight.frame.sequence == frame.sequence
            ):
                self._interactive_sequences.add(frame.sequence)
                self._emit(
                    "semantic_frame.admitted",
                    frame.sequence,
                    "interactive_inflight",
                )
                return SemanticAdmission(
                    admitted=True,
                    reason="interactive_inflight",
                    sequence=frame.sequence,
                )
            if self._pending is not None and self._pending.frame.sequence == frame.sequence:
                self._pending = replace(
                    self._pending,
                    priority="interactive",
                    pinned=True,
                )
                self._emit(
                    "semantic_frame.admitted",
                    frame.sequence,
                    "interactive_pending",
                )
                return SemanticAdmission(
                    admitted=True,
                    reason="interactive_pending",
                    sequence=frame.sequence,
                )
        retained = await asyncio.to_thread(self._retain_frame, frame)
        return await self._put(
            _SemanticFrameJob(
                frame=retained,
                priority="interactive",
                pinned=True,
            )
        )

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def close(self, *, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("semantic pipeline close timeout must be positive")
        async with self._lock:
            if self._closed:
                worker = self._worker
            else:
                self._closed = True
                if self._pending is not None:
                    await self._delete_owned(self._pending.frame)
                    self._pending = None
                self._notify_state()
                self._wake.set()
                worker = self._worker
        if worker is not None:
            if timeout_seconds is None:
                await worker
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    pass
        for path in list(self._owned_paths):
            await asyncio.to_thread(path.unlink, missing_ok=True)
            self._owned_paths.discard(path)
        self._idle.set()

    async def _put(self, job: _SemanticFrameJob) -> SemanticAdmission:
        async with self._lock:
            if self._closed:
                await self._delete_owned(job.frame)
                raise RuntimeError("semantic frame pipeline is closed")
            if (
                job.pinned
                and self._inflight is not None
                and self._inflight.frame.sequence == job.frame.sequence
            ):
                self._interactive_sequences.add(job.frame.sequence)
                await self._delete_owned(job.frame)
                self._emit(
                    "semantic_frame.admitted",
                    job.frame.sequence,
                    "interactive_inflight",
                )
                return SemanticAdmission(
                    admitted=True,
                    reason="interactive_inflight",
                    sequence=job.frame.sequence,
                )
            replaced_sequence: int | None = None
            if self._pending is not None:
                if self._pending.pinned:
                    await self._delete_owned(job.frame)
                    self._emit(
                        "semantic_frame.skipped",
                        job.frame.sequence,
                        "interactive_pending",
                    )
                    return SemanticAdmission(
                        admitted=False,
                        reason="interactive_pending",
                        sequence=job.frame.sequence,
                    )
                replaced_sequence = self._pending.frame.sequence
                await self._delete_owned(self._pending.frame)
            self._pending = job
            self._idle.clear()
            self._wake.set()
            self._ensure_worker()
            self._notify_state()
            if replaced_sequence is not None:
                self._emit(
                    "semantic_frame.replaced",
                    job.frame.sequence,
                    "latest_wins",
                    replaced_sequence=replaced_sequence,
                )
            self._emit(
                "semantic_frame.admitted",
                job.frame.sequence,
                "interactive" if job.pinned else "admitted",
            )
            return SemanticAdmission(
                admitted=True,
                reason=("interactive" if job.pinned else "admitted"),
                sequence=job.frame.sequence,
                replaced_sequence=replaced_sequence,
            )

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            job: _SemanticFrameJob | None = None
            async with self._lock:
                if self._pending is not None:
                    job = self._pending
                    self._pending = None
                    self._inflight = job
                    self._notify_state()
                else:
                    self._wake.clear()
                    if self._closed:
                        self._idle.set()
                        return
                    if self._inflight is None:
                        self._idle.set()
            if job is None:
                await self._wake.wait()
                continue
            try:
                await self._process(job)
            finally:
                async with self._lock:
                    self._inflight = None
                    self._notify_state()
                    if self._pending is None:
                        self._idle.set()

    async def _process(self, job: _SemanticFrameJob) -> None:
        observation = ImageObservation(
            session_id=self.coordinator.session_id,
            observation_id=f"video:{job.frame.video_id}:frame:{job.frame.sequence}",
            image_ref=job.frame.uri,
            video_id=job.frame.video_id,
            frame_sequence=job.frame.sequence,
            captured_at_ms=job.frame.timestamp_ms,
        )
        try:
            outcome = await asyncio.to_thread(
                self.coordinator.embed_image,
                observation,
                priority=job.priority,
            )
            if self._closed:
                await self._delete_owned(job.frame)
                return
            if isinstance(outcome, EmbeddingFailureEvent):
                await self._process_failure(
                    job,
                    force_interactive=(
                        job.pinned
                        or job.frame.sequence in self._interactive_sequences
                    ),
                )
                return
            if self.on_embedded is not None:
                try:
                    await self.on_embedded(job.frame, outcome)
                except Exception:
                    pass
            timestamp_seconds = _frame_timestamp_seconds(job.frame, self.clock)
            force_interactive = (
                job.pinned or job.frame.sequence in self._interactive_sequences
            )
            decision = self.selector.select(
                outcome,
                frame_timestamp_seconds=timestamp_seconds,
                force_interactive=force_interactive,
            )
            if not isinstance(decision, SemanticKeyframeDecision):
                raise TypeError("semantic selector returned a legacy decision")
            if decision.selected:
                await self._transfer_selected(
                    job.frame,
                    outcome,
                    decision.reason,
                    decision=decision,
                )
            else:
                self._emit(
                    "semantic_frame.skipped",
                    job.frame.sequence,
                    decision.reason,
                    decision=decision,
                )
                await self._delete_owned(job.frame)
        except asyncio.CancelledError:
            await self._delete_owned(job.frame)
            raise
        except Exception:
            self._emit(
                "semantic_frame.skipped",
                job.frame.sequence,
                "processing_error",
            )
            await self._delete_owned(job.frame)
        finally:
            self._interactive_sequences.discard(job.frame.sequence)

    async def _process_failure(
        self,
        job: _SemanticFrameJob,
        *,
        force_interactive: bool,
    ) -> None:
        timestamp_seconds = _frame_timestamp_seconds(job.frame, self.clock)
        if force_interactive:
            await self._transfer_selected(job.frame, None, "interactive")
        elif self.selector.force_due(timestamp_seconds):
            await self._transfer_selected(job.frame, None, "max_interval")
        else:
            self._emit(
                "semantic_frame.skipped",
                job.frame.sequence,
                "embedding_failed",
            )
            await self._delete_owned(job.frame)

    async def _transfer_selected(
        self,
        frame: VideoFrame,
        event: EmbeddingEvent | None,
        reason: str,
        *,
        decision: SemanticKeyframeDecision | None = None,
    ) -> None:
        try:
            await self.on_selected(frame, event, reason)
        except Exception:
            await self._delete_owned(frame)
            raise
        self._owned_paths.discard(Path(frame.uri))
        self._emit(
            "semantic_frame.selected",
            frame.sequence,
            reason,
            decision=decision,
        )

    def _retain_frame(self, frame: VideoFrame) -> VideoFrame:
        directory = self.retention_root / _safe_component(frame.video_id)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(frame.uri).suffix.lower() or ".jpg"
        destination = directory / f"frame-{frame.sequence:08d}-{uuid4().hex}{suffix}"
        try:
            os.link(frame.uri, destination)
        except OSError:
            try:
                shutil.copy2(frame.uri, destination)
            except OSError:
                destination.unlink(missing_ok=True)
                raise
        resolved = destination.resolve()
        self._owned_paths.add(resolved)
        return replace(frame, uri=str(resolved))

    async def _delete_owned(self, frame: VideoFrame) -> None:
        path = Path(frame.uri)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        self._owned_paths.discard(path)

    def _notify_state(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(
                1 if self._pending is not None else 0,
                self._inflight is not None,
            )

    def _emit(
        self,
        event_name: str,
        sequence: int,
        reason: str,
        *,
        replaced_sequence: int | None = None,
        decision: SemanticKeyframeDecision | None = None,
    ) -> None:
        emit_semantic_frame_observation(
            self.observer,
            event_name,
            session_id=self.coordinator.session_id,
            sequence=sequence,
            reason=reason,
            replaced_sequence=replaced_sequence,
            reference_sequence=(
                decision.reference_sequence if decision is not None else None
            ),
            semantic_similarity=(
                decision.semantic_similarity if decision is not None else None
            ),
            semantic_change=(
                decision.semantic_change if decision is not None else None
            ),
            semantic_threshold=(
                self.selector.config.semantic_threshold
                if decision is not None
                else None
            ),
            selected=(decision.selected if decision is not None else None),
        )


def _frame_timestamp_seconds(frame: VideoFrame, clock: Callable[[], float]) -> float:
    if frame.timestamp_ms is not None:
        return frame.timestamp_ms / 1000.0
    return clock()


def _safe_component(value: str) -> str:
    normalized = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    return normalized[:120] or "video"
