"""Local-only adaptive keyframe collection for realtime video streams."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.video.detection.frame_difference import FrameDifferenceDetector
from assistant_agent.media.video.detection.semantic_detector import SemanticChangeDetector, create_semantic_change_detector
from assistant_agent.media.video.detection.ssim_detector import SSIMChangeDetector
from assistant_agent.media.video.keyframe.selector import KeyframeSelectorConfig, SemanticKeyframeSelector
from assistant_agent.media.video.sampling.adaptive_sampler import AdaptiveFrameSampler, AdaptiveSamplerConfig
from assistant_agent.media.video.types import FrameProcessingResult, KeyframeChangeMetrics, VideoFrame


@dataclass(frozen=True)
class KeyframeCollectionResult:
    """Local selection result with an optional chosen frame."""

    processing: FrameProcessingResult
    selected_frame: VideoFrame | None


class AdaptiveKeyframeCollector:
    """Select keyframes without invoking an external understanding model."""

    def __init__(
        self,
        *,
        sampler_config: AdaptiveSamplerConfig | None = None,
        keyframe_config: KeyframeSelectorConfig | None = None,
        frame_difference_detector: FrameDifferenceDetector | None = None,
        ssim_detector: SSIMChangeDetector | None = None,
        semantic_detector: SemanticChangeDetector | None = None,
        semantic_probe_fps: float | None = None,
        embedding_coordinator: SessionEmbeddingCoordinator | None = None,
        config: ProviderConfig | None = None,
    ) -> None:
        self.sampler = AdaptiveFrameSampler(sampler_config)
        self.selector = SemanticKeyframeSelector(keyframe_config)
        self.frame_difference_detector = frame_difference_detector or FrameDifferenceDetector()
        self.ssim_detector = ssim_detector or SSIMChangeDetector()
        self.semantic_detector = semantic_detector or (
            create_semantic_change_detector(
                config,
                coordinator=embedding_coordinator,
            )
            if config is not None or embedding_coordinator is not None
            else SemanticChangeDetector()
        )
        self.semantic_probe_fps = (
            semantic_probe_fps
            if semantic_probe_fps is not None
            else config.keyframe_semantic_probe_fps
            if config is not None
            else 2.0
        )
        if self.semantic_probe_fps <= 0:
            raise ValueError("semantic probe FPS must be positive")
        self.log_records: list[dict[str, Any]] = []
        self._last_keyframe: VideoFrame | None = None
        self._last_keyframe_at: float | None = None
        self._last_semantic_probe_at: float | None = None

    def collect(self, frame: VideoFrame) -> KeyframeCollectionResult:
        """Evaluate one frame and return immediately without MLLM work."""

        started_at = time.perf_counter()
        force = self.selector.force_due(frame.timestamp_seconds, self._last_keyframe_at)
        metrics, errors = self._change_metrics(frame, force=force)
        decision = self.selector.select(
            frame,
            metrics,
            last_keyframe_at=self._last_keyframe_at,
        )
        sampling = self.sampler.should_sample(
            timestamp_seconds=frame.timestamp_seconds,
            change_score=0.0 if self._last_keyframe is None else metrics.change_score,
            force=force or decision.selected,
        )
        selected_frame: VideoFrame | None = None
        reason = sampling.reason
        if sampling.sampled:
            reason = decision.reason
            if decision.selected:
                selected_frame = frame
                self.semantic_detector.commit_current_embedding_as_keyframe(frame)
                self._last_keyframe = frame
                self._last_keyframe_at = frame.timestamp_seconds

        processing = FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=frame.timestamp_seconds,
            sampled=sampling.sampled,
            sampling_rate=sampling.sampling_rate,
            metrics=metrics,
            keyframe_selected=selected_frame is not None,
            qwen_called=False,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            decision_reason=reason,
            errors=errors,
        )
        self.log_records.append(_log_record(processing))
        return KeyframeCollectionResult(processing=processing, selected_frame=selected_frame)

    def _change_metrics(
        self,
        frame: VideoFrame,
        *,
        force: bool,
    ) -> tuple[KeyframeChangeMetrics, list[dict[str, Any]]]:
        pixel = self.frame_difference_detector.compare(frame, self._last_keyframe)
        structural = self.ssim_detector.compare(frame, self._last_keyframe)
        probe_interval = 1.0 / self.semantic_probe_fps
        probe_due = (
            self._last_semantic_probe_at is None
            or frame.timestamp_seconds - self._last_semantic_probe_at + 1e-9
            >= probe_interval
        )
        semantic_candidate = (
            self._last_keyframe is None
            or structural.structural_change_score
            >= self.selector.config.structural_threshold
            or probe_due
            or force
        )
        if semantic_candidate:
            self._last_semantic_probe_at = frame.timestamp_seconds
        semantic = self.semantic_detector.compare(
            frame,
            self._last_keyframe,
            semantic_candidate=semantic_candidate,
        )
        metrics = self.selector.with_score(
            KeyframeChangeMetrics(
                pixel_change_score=pixel.pixel_change_score,
                structural_change_score=structural.structural_change_score,
                semantic_change_score=semantic.semantic_change_score,
                object_change_score=pixel.object_change_score,
            )
        )
        return metrics, semantic.errors


def _log_record(result: FrameProcessingResult) -> dict[str, Any]:
    return {
        "timestamp": result.timestamp_seconds,
        "frame_id": result.frame_id,
        "sampling_rate": result.sampling_rate,
        "change_score": result.metrics.keyframe_score,
        "keyframe_selected": result.keyframe_selected,
        "qwen_called": result.qwen_called,
        "latency_ms": result.latency_ms,
        "errors": result.errors,
    }
