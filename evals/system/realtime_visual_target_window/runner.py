"""Operator-gated real VLM eval for a logical-keyframe target window."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter_ns
from typing import Any
from uuid import uuid4

from assistant_agent.config import AppConfig, ProviderConfig, load_app_config
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.visual_perception.module import VisualPerceptionSession  # noqa: F401
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.video_context import REALTIME_VISUAL_TARGET_WINDOW_SIZE, VideoFrame
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.media.vision.vision_client import create_vision_understanding_client
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationOutcome,
    RealtimeVisualObservationRequest,
    RealtimeVisualObservationService,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)
from assistant_agent.tools.runtime import ToolContext


DEFAULT_OUTPUT_ROOT = Path(".data/evals/system/realtime_visual_target_window")


class RealtimeVisualEvalConfigurationError(RuntimeError):
    """The real window evaluation is not explicitly and fully configured."""


def dry_run_report(*, frame_dir: Path | None, allow_real_provider: bool) -> dict[str, object]:
    """Describe the gated real evaluation without reading frames or calling Provider."""

    config = ProviderConfig.from_env()
    configured_frames = _frame_candidates(frame_dir) if frame_dir is not None else []
    return {
        "status": "dry_run",
        "real_provider_authorized": bool(allow_real_provider),
        "provider_mode": config.provider_mode,
        "vision_provider": config.vision_provider,
        "vision_config_complete": _qwen_vision_config_complete(config),
        "frame_dir_configured": frame_dir is not None,
        "candidate_frame_count": len(configured_frames),
        "planned_provider_calls": "1 closed keyframe window (1..5 images)",
        "would_check": [
            "one_multiframe_vlm_call_for_frozen_window",
            "ordered_keyframes_end_at_exact_target",
            "window_uses_isolated_native_multimodal_call",
            "exact_target_barrier",
            "exact_target_trace_correlation",
        ],
        "network_called": False,
    }


def run_real_eval(
    *,
    frame_dir: Path,
    allow_real_provider: bool,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> tuple[Path, dict[str, object]]:
    """Run one full window of isolated frame observations and the target barrier."""

    config = ProviderConfig.from_env()
    _validate_real_eval(config, allow_real_provider=allow_real_provider)
    frame_paths = _validated_frame_paths(frame_dir)
    result = asyncio.run(
        _run_window(
            config=config,
            app_config=load_app_config(),
            frame_paths=frame_paths,
        )
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, result


async def _run_window(
    *,
    config: ProviderConfig,
    app_config: AppConfig,
    frame_paths: list[tuple[int, Path]],
) -> dict[str, object]:
    user_id = "realtime-visual-system-eval"
    session_id = f"system-eval-{uuid4().hex}"
    video_id = f"video-{uuid4().hex}"
    registry = _ObservationRegistry()
    trace_store = InMemoryTraceStore()
    memory_store = RealtimeVideoMemoryStore()
    trace_links: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="realtime-visual-window-eval-") as temporary:
        temp_root = Path(temporary)
        semantic_store = SessionVisualSemanticStore(
            root=temp_root / "semantic-store",
            session_id=session_id,
        )

        def service_factory() -> _MeasuredObservationService:
            service_id = registry.create_service()
            return _MeasuredObservationService(
                service_id=service_id,
                registry=registry,
                delegate=RealtimeVisualObservationService(
                    client=create_vision_understanding_client(
                        app_config.vision,
                        provider_mode=app_config.provider_mode,
                        media_config=app_config.media,
                    )
                ),
            )

        observer = RealtimeVideoObserver(
            user_id=user_id,
            session_id=session_id,
            observation_service_factory=service_factory,
            memory_store=memory_store,
            semantic_store=semantic_store,
            embedding_coordinator=SessionEmbeddingCoordinator(
                session_id, MockMultimodalEmbeddingProvider()
            ),
            visual_memory_text_index=UnavailableVisualMemoryTextIndex(
                code="system_eval_index_disabled",
                message="visual-memory indexing is outside this system eval",
            ),
            trace_store=trace_store,
            vision_config=app_config.vision,
            provider_mode=app_config.provider_mode,
            keyframe_root=temp_root / "keyframes",
        )
        frames = tuple(
            VideoFrame(
                video_id=video_id,
                frame_id=f"frame-{sequence}",
                uri=str(path),
                sequence=sequence,
                timestamp_ms=sequence * 200,
            )
            for sequence, path in frame_paths
        )
        branch = VideoUnderstandingBranch(
            client=create_vision_understanding_client(
                app_config.vision,
                provider_mode=app_config.provider_mode,
                media_config=app_config.media,
            ),
            memory_store=memory_store,
            semantic_store_pool=_SingleSemanticStorePool(semantic_store),
        )
        visual_session = VisualPerceptionSession(
            observer=observer,
            video_context_store=object(),
            release=lambda _session: None,
        )
        try:
            for frame in frames:
                await observer.submit(frame)
            await observer.semantic_pipeline.wait_idle()
            frozen_window = await visual_session.prepare_strict_window([video_id])
            if frozen_window is None:
                raise RuntimeError("semantic selector produced no logical keyframe")
            logical_sequences = frozen_window.sequences
            start_sequence = frozen_window.start_sequence
            target_sequence = frozen_window.target_sequence
            context = ToolContext(
                user_id=user_id,
                session_id=session_id,
                run_id=f"run-{uuid4().hex}",
                trace_id=f"trace-{uuid4().hex}",
                trace_store=trace_store,
                metadata={
                    "entry_profile": "agent_service",
                    "visual_window_id": frozen_window.window_id,
                    "visual_window_start_sequence": start_sequence,
                    "visual_target_sequence": target_sequence,
                    "visual_window_sequences": logical_sequences,
                },
            )
            tool_started_ns = perf_counter_ns()
            tool_result = await asyncio.to_thread(
                branch.execute,
                VideoUnderstandingRequest(video_ref=video_id),
                context,
            )
            tool_returned_ns = perf_counter_ns()
            await observer.wait_idle()
            trace_links = [
                {
                    "sequence": record.frame_sequence,
                    "trace_id": record.source_vision_trace_id,
                    "run_id": record.source_vision_run_id,
                    "span_id": record.source_vlm_span_id,
                }
                for record in semantic_store.records_in_sequence_range(
                    video_id,
                    start_sequence=start_sequence,
                    end_sequence=target_sequence,
                )
            ]
        finally:
            await observer.close()
            semantic_store.close()

    observations = registry.snapshot()
    result_data = tool_result.data if isinstance(tool_result.data, dict) else {}
    target_finished_ns = registry.finished_ns(target_sequence)
    service_ids = [str(item["connection_id"]) for item in observations]
    executed_sequences = {
        int(item["sequence"])
        for item in observations
        if isinstance(item.get("sequence"), int)
    }
    ready_sequences = {
        int(sequence)
        for sequence in result_data.get("ready_sequences", [])
        if isinstance(sequence, int) and not isinstance(sequence, bool)
    }
    checks = {
        "one_window_call_finished": len(observations) == 1,
        "isolated_connection": len(service_ids) == len(set(service_ids)) == 1,
        "ordered_window_exact": (
            observations[0].get("frame_sequences") == list(logical_sequences)
            if observations
            else False
        ),
        "target_executed": target_sequence in executed_sequences,
        "exact_target_ready": result_data.get("target_ready") is True,
        "ready_is_exact_target": ready_sequences == {target_sequence},
        "exact_target_trace_linked": len(trace_links) == 1
        and trace_links[0].get("sequence") == target_sequence
        and all(
            isinstance(trace_links[0].get(field), str)
            and bool(trace_links[0].get(field))
            for field in ("trace_id", "run_id", "span_id")
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "passed": all(checks.values()),
        "checks": checks,
        "window_start_sequence": start_sequence,
        "target_sequence": target_sequence,
        "logical_sequences": list(logical_sequences),
        "frames": observations,
        "max_concurrency": registry.max_concurrency,
        "target_wait_ms": max(0, (tool_returned_ns - tool_started_ns) // 1_000_000),
        "target_finish_to_tool_return_ms": (
            max(0, (tool_returned_ns - target_finished_ns) // 1_000_000)
            if target_finished_ns is not None
            else None
        ),
        "ready_sequences": result_data.get("ready_sequences", []),
        "missing_sequences": result_data.get("missing_sequences", []),
        "trace_links": trace_links,
    }


@dataclass
class _ObservationTiming:
    sequence: int
    frame_sequences: tuple[int, ...]
    connection_id: str
    started_ns: int
    finished_ns: int | None = None
    status: str = "started"


class _ObservationRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[int, _ObservationTiming] = {}
        self._active = 0
        self.max_concurrency = 0

    def create_service(self) -> str:
        return f"connection-{uuid4().hex}"

    def started(
        self,
        *,
        sequence: int,
        frame_sequences: tuple[int, ...],
        connection_id: str,
    ) -> None:
        with self._lock:
            self._items[sequence] = _ObservationTiming(
                sequence=sequence,
                frame_sequences=frame_sequences,
                connection_id=connection_id,
                started_ns=perf_counter_ns(),
            )
            self._active += 1
            self.max_concurrency = max(self.max_concurrency, self._active)

    def finished(self, *, sequence: int, status: str) -> None:
        with self._lock:
            item = self._items[sequence]
            item.finished_ns = perf_counter_ns()
            item.status = status
            self._active -= 1

    def finished_ns(self, sequence: int) -> int | None:
        with self._lock:
            item = self._items.get(sequence)
            return item.finished_ns if item is not None else None

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {
                    "sequence": item.sequence,
                    "frame_sequences": list(item.frame_sequences),
                    "connection_id": item.connection_id,
                    "status": item.status,
                    "started_ns": item.started_ns,
                    "finished_ns": item.finished_ns,
                    "latency_ms": (
                        max(0, (item.finished_ns - item.started_ns) // 1_000_000)
                        if item.finished_ns is not None
                        else None
                    ),
                }
                for item in sorted(self._items.values(), key=lambda value: value.sequence)
            ]


class _MeasuredObservationService:
    def __init__(
        self,
        *,
        service_id: str,
        registry: _ObservationRegistry,
        delegate: RealtimeVisualObservationService,
    ) -> None:
        self.service_id = service_id
        self.registry = registry
        self.delegate = delegate

    def observe(
        self,
        request: RealtimeVisualObservationRequest,
        *,
        trace_context: Any = None,
    ) -> RealtimeVisualObservationOutcome:
        self.registry.started(
            sequence=request.frame_sequence,
            frame_sequences=request.frame_sequences,
            connection_id=self.service_id,
        )
        status = "failed"
        try:
            outcome = self.delegate.observe(request, trace_context=trace_context)
            status = "succeeded" if outcome.succeeded else "failed"
            return outcome
        finally:
            self.registry.finished(sequence=request.frame_sequence, status=status)

    def close(self) -> None:
        self.delegate.close()


class _SingleSemanticStorePool:
    def __init__(self, store: SessionVisualSemanticStore) -> None:
        self.store = store

    def peek(self, _user_id: str, _session_id: str) -> SessionVisualSemanticStore:
        return self.store


def _validate_real_eval(config: ProviderConfig, *, allow_real_provider: bool) -> None:
    if not allow_real_provider:
        raise RealtimeVisualEvalConfigurationError(
            "real Provider calls require --allow-real-provider"
        )
    if config.provider_mode != "real":
        raise RealtimeVisualEvalConfigurationError(
            "MULTIMODAL_AGENT_PROVIDER_MODE must be real"
        )
    if config.vision_provider != "qwen":
        raise RealtimeVisualEvalConfigurationError(
            "realtime visual eval requires MULTIMODAL_AGENT_VISION_PROVIDER=qwen"
        )
    if not _qwen_vision_config_complete(config):
        raise RealtimeVisualEvalConfigurationError(
            "Qwen native multimodal vision configuration is incomplete"
        )


def _qwen_vision_config_complete(config: ProviderConfig) -> bool:
    resolved = config.resolved_vision_provider()
    return resolved.adapter_kind == "dashscope_multimodal" and not resolved.missing_required_env()


def _validated_frame_paths(frame_dir: Path) -> list[tuple[int, Path]]:
    paths = _frame_candidates(frame_dir)
    if len(paths) != REALTIME_VISUAL_TARGET_WINDOW_SIZE:
        raise RealtimeVisualEvalConfigurationError(
            "frame-dir must contain exactly "
            f"{REALTIME_VISUAL_TARGET_WINDOW_SIZE} sequence-named JPEG files"
        )
    sequences = [sequence for sequence, _path in paths]
    if len(set(sequences)) != len(sequences):
        raise RealtimeVisualEvalConfigurationError("frame sequences must be unique")
    if any(
        next_sequence != sequence + 1
        for sequence, next_sequence in zip(sequences, sequences[1:])
    ):
        raise RealtimeVisualEvalConfigurationError("frame sequences must be consecutive")
    return paths


def _frame_candidates(frame_dir: Path | None) -> list[tuple[int, Path]]:
    if frame_dir is None or not frame_dir.is_dir():
        return []
    candidates: list[tuple[int, Path]] = []
    for path in frame_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        try:
            sequence = int(path.stem)
        except ValueError:
            continue
        if sequence >= 0:
            candidates.append((sequence, path))
    return sorted(candidates)
