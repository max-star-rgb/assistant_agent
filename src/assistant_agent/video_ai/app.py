"""Orchestration for adaptive realtime video understanding."""

from __future__ import annotations

import time
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.video_ai.detection.frame_difference import FrameDifferenceDetector
from assistant_agent.video_ai.detection.semantic_detector import SemanticChangeDetector, create_semantic_change_detector
from assistant_agent.video_ai.detection.ssim_detector import SSIMChangeDetector
from assistant_agent.video_ai.keyframe.selector import KeyframeSelectorConfig, SemanticKeyframeSelector
from assistant_agent.video_ai.keyframe.storage import FileKeyframeStorage, KeyframeStorage
from assistant_agent.video_ai.memory.state_manager import VideoMemoryStateManager
from assistant_agent.video_ai.qwen.vision_client import MockQwenVisionClient, VisionUnderstandingClient
from assistant_agent.video_ai.sampling.adaptive_sampler import AdaptiveFrameSampler, AdaptiveSamplerConfig
from assistant_agent.video_ai.types import FrameProcessingResult, KeyframeChangeMetrics, QueryAnswer, VideoFrame


class RealtimeVideoUnderstandingApp:
    """Adaptive observer that only calls Qwen-VL for selected keyframes."""

    def __init__(
        self,
        *,
        qwen_client: VisionUnderstandingClient | None = None,
        memory: VideoMemoryStateManager | None = None,
        sampler_config: AdaptiveSamplerConfig | None = None,
        keyframe_config: KeyframeSelectorConfig | None = None,
        frame_difference_detector: FrameDifferenceDetector | None = None,
        ssim_detector: SSIMChangeDetector | None = None,
        semantic_detector: SemanticChangeDetector | None = None,
        keyframe_storage: KeyframeStorage | None = None,
        config: ProviderConfig | None = None,
    ) -> None:
        self.qwen_client = qwen_client or MockQwenVisionClient()
        self.memory = memory or VideoMemoryStateManager()
        self.sampler = AdaptiveFrameSampler(sampler_config)
        self.selector = SemanticKeyframeSelector(keyframe_config)
        self.frame_difference_detector = frame_difference_detector or FrameDifferenceDetector()
        self.ssim_detector = ssim_detector or SSIMChangeDetector()
        self.semantic_detector = semantic_detector or (
            create_semantic_change_detector(config) if config is not None else SemanticChangeDetector()
        )
        self.keyframe_storage = keyframe_storage or FileKeyframeStorage()
        self.log_records: list[dict[str, Any]] = []
        self._last_keyframe: VideoFrame | None = None
        self._last_keyframe_at: float | None = None

    def process_frame(self, frame: VideoFrame) -> FrameProcessingResult:
        """Process one frame from the continuous video stream."""

        started_at = time.perf_counter()
        metrics, errors = self._change_metrics(frame)
        force_sample = self.selector.force_due(frame.timestamp_seconds, self._last_keyframe_at)
        sampler_score = 0.0 if self._last_keyframe is None else metrics.change_score
        sampling = self.sampler.should_sample(
            timestamp_seconds=frame.timestamp_seconds,
            change_score=sampler_score,
            force=force_sample,
        )
        qwen_called = False
        selected = False
        reason = sampling.reason

        if sampling.sampled:
            decision = self.selector.select(frame, metrics, last_keyframe_at=self._last_keyframe_at)
            selected = decision.selected
            reason = decision.reason
            if selected:
                stored_frame = self.keyframe_storage.store(frame)
                observation = self.qwen_client.understand_keyframe(
                    stored_frame,
                    self.memory.recent_keyframes(),
                    self.memory.current_state,
                )
                qwen_called = True
                self.memory.apply_observation(stored_frame, observation)
                self.semantic_detector.commit_current_embedding_as_keyframe(stored_frame)
                self._last_keyframe = stored_frame
                self._last_keyframe_at = stored_frame.timestamp_seconds

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        result = FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=frame.timestamp_seconds,
            sampled=sampling.sampled,
            sampling_rate=sampling.sampling_rate,
            metrics=metrics,
            keyframe_selected=selected,
            qwen_called=qwen_called,
            latency_ms=latency_ms,
            decision_reason=reason,
            errors=errors,
        )
        self._log(result)
        return result

    def answer_query(self, query: str) -> QueryAnswer:
        """Answer from rolling state and recent keyframes without rescanning video."""

        return self.qwen_client.answer_query(query, self.memory.snapshot(), self.memory.recent_keyframes())

    def _change_metrics(self, frame: VideoFrame) -> tuple[KeyframeChangeMetrics, list[dict[str, Any]]]:
        pixel = self.frame_difference_detector.compare(frame, self._last_keyframe)
        structural = self.ssim_detector.compare(frame, self._last_keyframe)
        semantic_candidate = (
            self._last_keyframe is None
            or pixel.pixel_change_score >= self.selector.config.threshold
            or structural.structural_change_score >= self.selector.config.threshold
        )
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

    def _log(self, result: FrameProcessingResult) -> None:
        self.log_records.append(
            {
                "timestamp": result.timestamp_seconds,
                "frame_id": result.frame_id,
                "sampling_rate": result.sampling_rate,
                "change_score": result.metrics.keyframe_score,
                "keyframe_selected": result.keyframe_selected,
                "qwen_called": result.qwen_called,
                "latency_ms": result.latency_ms,
                "errors": result.errors,
            }
        )
