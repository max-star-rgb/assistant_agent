"""Qwen-VL adapter for bounded frame-based video understanding."""

from __future__ import annotations

from typing import Any

from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.video_adapter import _failed_result
from assistant_agent.video_ai.memory.state_manager import KeyframeMemoryRecord
from assistant_agent.video_ai.qwen.vision_client import (
    QwenVLClient,
    QwenVLConfig,
    VisionUnderstandingClient,
)
from assistant_agent.video_ai.types import VideoFrame


class QwenVideoUnderstandingAdapter:
    """Map the stable video contract onto Qwen-VL multi-image analysis."""

    provider = "qwen"

    def __init__(
        self,
        config: QwenVLConfig,
        *,
        client: VisionUnderstandingClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or QwenVLClient(config)

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        if not request.video_ref:
            raise ValueError("video_missing_input: VideoUnderstandingRequest requires video_ref.")
        if not request.frame_refs:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="video_missing_frames",
                message="qwen video provider requires frame_refs from the video context window.",
                recoverable=True,
            )

        current_timestamp = _current_timestamp_seconds(request)
        history = [
            KeyframeMemoryRecord(
                frame_id=f"history-frame-{index + 1}",
                timestamp_seconds=float(index),
                uri=uri,
                summary="",
                scene="",
                objects=[],
                people=[],
            )
            for index, uri in enumerate(request.frame_refs[:-1])
        ]
        current = VideoFrame(
            frame_id=_current_frame_id(request),
            timestamp_seconds=current_timestamp,
            uri=request.frame_refs[-1],
            metadata={"video_ref": request.video_ref},
        )
        previous_state = request.memory_context if isinstance(request.memory_context, str) else ""
        try:
            observation = self.client.understand_keyframe(current, history, previous_state)
        except Exception as exc:  # noqa: BLE001 - Provider boundary.
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_bad_response",
                message=sanitize_error_message(exc),
                recoverable=True,
            )

        return VideoUnderstandingResult(
            summary=observation.summary or "Qwen 已完成视频关键帧理解。",
            objects=list(observation.objects),
            people=list(observation.people),
            actions=list(observation.actions),
            events=list(observation.important_events),
            scene=observation.scene or None,
            provider=self.provider,
            model=self.config.model,
            output_ref=f"provider://video/qwen/{_safe_ref_suffix(request.video_ref)}",
            errors=[dict(error) for error in observation.errors],
            latency_ms=observation.latency_ms,
        )


def _current_timestamp_seconds(request: VideoUnderstandingRequest) -> float:
    value: Any = request.metadata.get("frame_timestamp_ms")
    if isinstance(value, int | float):
        return max(0.0, float(value) / 1000.0)
    return float(max(0, len(request.frame_refs) - 1))


def _current_frame_id(request: VideoUnderstandingRequest) -> str:
    value = request.metadata.get("frame_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"frame-{len(request.frame_refs)}"


def _safe_ref_suffix(video_ref: str) -> str:
    suffix = video_ref.rsplit("/", maxsplit=1)[-1].strip() or "video"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in suffix)
