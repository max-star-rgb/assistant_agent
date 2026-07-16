"""Orchestration for adaptive realtime video understanding."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.video_ai.detection.frame_difference import FrameDifferenceDetector
from assistant_agent.video_ai.detection.semantic_detector import SemanticChangeDetector
from assistant_agent.video_ai.detection.ssim_detector import SSIMChangeDetector
from assistant_agent.video_ai.keyframe.collector import AdaptiveKeyframeCollector
from assistant_agent.video_ai.keyframe.selector import KeyframeSelectorConfig
from assistant_agent.video_ai.keyframe.storage import FileKeyframeStorage, KeyframeStorage
from assistant_agent.video_ai.local_vision_client import MockRealtimeVisionClient, VisionUnderstandingClient
from assistant_agent.video_ai.memory.state_manager import VideoMemoryStateManager
from assistant_agent.video_ai.sampling.adaptive_sampler import AdaptiveSamplerConfig
from assistant_agent.video_ai.types import FrameProcessingResult, QueryAnswer, VideoFrame


class RealtimeVideoUnderstandingApp:
    """Adaptive observer that only calls the local vision client for selected keyframes."""

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
        self.qwen_client = qwen_client or MockRealtimeVisionClient()
        self.memory = memory or VideoMemoryStateManager()
        self.collector = AdaptiveKeyframeCollector(
            sampler_config=sampler_config,
            keyframe_config=keyframe_config,
            frame_difference_detector=frame_difference_detector,
            ssim_detector=ssim_detector,
            semantic_detector=semantic_detector,
            config=config,
        )
        self.sampler = self.collector.sampler
        self.selector = self.collector.selector
        self.frame_difference_detector = self.collector.frame_difference_detector
        self.ssim_detector = self.collector.ssim_detector
        self.semantic_detector = self.collector.semantic_detector
        self.keyframe_storage = keyframe_storage or FileKeyframeStorage()
        self.log_records: list[dict[str, Any]] = []

    def process_frame(self, frame: VideoFrame) -> FrameProcessingResult:
        """Process one frame from the continuous video stream."""

        started_at = time.perf_counter()
        collection = self.collector.collect(frame)
        result = collection.processing
        if collection.selected_frame is not None:
            stored_frame = self.keyframe_storage.store(collection.selected_frame)
            observation = self.qwen_client.understand_keyframe(
                stored_frame,
                self.memory.recent_keyframes(),
                self.memory.current_state,
            )
            self.memory.apply_observation(stored_frame, observation)
            result = replace(
                result,
                qwen_called=True,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
        self._log(result)
        return result

    def answer_query(self, query: str) -> QueryAnswer:
        """Answer from rolling state and recent keyframes without rescanning video."""

        return self.qwen_client.answer_query(query, self.memory.snapshot(), self.memory.recent_keyframes())

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
