"""Video understanding adapter contract and deterministic mock implementation."""

from pathlib import Path
from typing import Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult


class VideoUnderstandingAdapter(Protocol):
    """Adapter contract for external Video MLLM / VLM providers."""

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        """Return structured video understanding output."""


class MockVideoUnderstandingAdapter:
    """Deterministic local video adapter for tests and offline demo flows."""

    provider = "mock"

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        if not request.video_ref:
            raise ValueError("video_missing_input: VideoUnderstandingRequest requires video_ref.")
        frame_refs = list(request.frame_refs)
        frame_count = len(frame_refs)

        return VideoUnderstandingResult(
            summary=(
                f"视频中展示了一双白色低帮运动鞋，整体为简约日系商品展示风格。"
                f" 已基于最近 {frame_count} 帧上下文进行理解。"
                if frame_count
                else "视频中展示了一双白色低帮运动鞋，整体为简约日系商品展示风格。"
            ),
            objects=["白色低帮运动鞋", "桌面"],
            actions=["商品展示", "鞋身旋转"],
            events=["展示鞋面", "展示鞋底"],
            scene="室内商品展示场景",
            products=["白色低帮运动鞋"],
            brands=[],
            colors=["白色"],
            materials=["皮革", "橡胶"],
            text_in_video=[],
            timestamps=_mock_timestamps(frame_refs),
            style_tags=["简约", "日系"],
            confidence=0.9,
            provider=self.provider,
            model="mock-video-understanding",
            output_ref=f"mock://video/understanding/{_safe_ref_suffix(request.video_ref)}",
            errors=[],
            latency_ms=1,
        )


class FakeRealtimeVisionAdapter:
    """Deterministic fake realtime provider used to verify provider replaceability."""

    provider = "fake_realtime"

    def __init__(self, *, model: str = "fake-realtime-vision") -> None:
        self.model = model
        self.last_observation_diagnostics: dict[str, object] = {}

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        video_ref = request.video_ref or (request.video_ids[0] if request.video_ids else None)
        if not video_ref:
            raise ValueError("video_missing_input: VideoUnderstandingRequest requires video_ref.")
        sequence = _request_sequence(request)
        self.last_observation_diagnostics = {
            "transport": "fake_realtime",
            "session_generation": 1,
            "connection_reused": True,
            "reconnect_count": 0,
            "target_sequence": sequence,
            "completed_sequence": sequence,
            "first_delta_latency_ms": 0,
            "total_observation_latency_ms": 1,
        }
        return VideoUnderstandingResult(
            summary=f"fake realtime provider observed {video_ref} at keyframe {sequence}.",
            objects=["fake realtime object"],
            actions=["keyframe observation"],
            events=["realtime keyframe processed"],
            scene="fake realtime scene",
            provider=self.provider,
            model=self.model,
            output_ref=f"fake://realtime-video/{_safe_ref_suffix(video_ref)}/{sequence}",
            errors=[],
            latency_ms=1,
        )


def create_video_understanding_adapter(config: ProviderConfig | None = None) -> VideoUnderstandingAdapter:
    """Create a video understanding adapter from the selected vision provider."""

    resolved = config or ProviderConfig.from_env()
    provider = resolved.resolved_vision_provider()
    if provider.adapter_kind == "fake_realtime_vision":
        return FakeRealtimeVisionAdapter(model=provider.model or "fake-realtime-vision")
    if resolved.vision_provider == "qwen":
        return _create_qwen_realtime_adapter(resolved)
    return MockVideoUnderstandingAdapter()


def create_realtime_video_understanding_adapter(
    config: ProviderConfig | None = None,
) -> VideoUnderstandingAdapter:
    """Select Qwen realtime only for background live-video observations."""

    resolved = config or ProviderConfig.from_env()
    return create_video_understanding_adapter(resolved)


def _create_qwen_realtime_adapter(resolved: ProviderConfig) -> VideoUnderstandingAdapter:
    from assistant_agent.providers.qwen_realtime_vision import (
        QwenRealtimeVisionAdapter,
        QwenRealtimeVisionConfig,
    )

    return QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(
            api_key=resolved.qwen_realtime_vision_api_key,
            base_url=resolved.qwen_realtime_vision_base_url,
            model=resolved.qwen_realtime_vision_model,
            timeout_seconds=resolved.video_understanding_timeout_seconds,
        )
    )


def _safe_ref_suffix(video_ref: str) -> str:
    suffix = video_ref.rsplit("/", maxsplit=1)[-1].strip() or "demo"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in suffix)


def _mock_timestamps(frame_refs: list[str]) -> list[dict]:
    if not frame_refs:
        return [{"start_ms": 0, "end_ms": 3000, "description": "展示白色低帮运动鞋"}]
    return [
        {
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "description": f"处理上下文帧 {index + 1}",
            "frame_ref": Path(frame_ref).name,
        }
        for index, frame_ref in enumerate(frame_refs)
    ]


def _request_sequence(request: VideoUnderstandingRequest) -> int:
    sequence = request.metadata.get("frame_sequence")
    if isinstance(sequence, int):
        return sequence
    if isinstance(sequence, str) and sequence.isdigit():
        return int(sequence)
    if request.frame_refs:
        stem = Path(request.frame_refs[-1]).stem
        suffix = stem.rsplit("-", maxsplit=1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return 1
