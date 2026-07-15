"""Video understanding adapter contract and deterministic mock implementation."""

from pathlib import Path
from typing import Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.services.provider_errors import build_provider_error


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


class HttpVideoUnderstandingAdapter:
    """HTTP Video MLLM provider skeleton.

    The skeleton deliberately performs no network IO. It validates provider
    configuration and safety limits, then returns structured provider state.
    """

    provider = "http"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "video-understanding",
        timeout_seconds: float = 60.0,
        max_video_bytes: int = 52_428_800,
        max_video_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_video_bytes = max_video_bytes
        self.max_video_seconds = max_video_seconds

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        if not request.video_ref:
            raise ValueError("video_missing_input: VideoUnderstandingRequest requires video_ref.")
        size_error = self._size_error(request)
        if size_error is not None:
            return size_error
        duration_error = self._duration_error(request)
        if duration_error is not None:
            return duration_error
        if not self.base_url:
            return _failed_result(
                provider=self.provider,
                model=self.model,
                code="provider_unconfigured",
                message="http video provider is missing VIDEO_UNDERSTANDING_BASE_URL.",
                recoverable=True,
            )
        if not self.api_key:
            return _failed_result(
                provider=self.provider,
                model=self.model,
                code="provider_unconfigured",
                message="http video provider is missing VIDEO_UNDERSTANDING_API_KEY.",
                recoverable=True,
            )
        return _failed_result(
            provider=self.provider,
            model=self.model,
            code="video_provider_unavailable",
            message="HTTP video provider skeleton is configured but no real client is enabled.",
            recoverable=True,
        )

    def _size_error(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult | None:
        size_bytes = _number_metadata(request, "size_bytes", "video_bytes")
        if size_bytes is not None and size_bytes > self.max_video_bytes:
            return _failed_result(
                provider=self.provider,
                model=self.model,
                code="video_file_too_large",
                message="Video size exceeds MULTIMODAL_AGENT_MAX_VIDEO_BYTES.",
                recoverable=True,
            )
        return None

    def _duration_error(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult | None:
        duration_seconds = _number_metadata(request, "duration_seconds", "video_seconds")
        if duration_seconds is not None and duration_seconds > self.max_video_seconds:
            return _failed_result(
                provider=self.provider,
                model=self.model,
                code="video_file_too_large",
                message="Video duration exceeds MULTIMODAL_AGENT_MAX_VIDEO_SECONDS.",
                recoverable=True,
            )
        return None


def create_video_understanding_adapter(config: ProviderConfig | None = None) -> VideoUnderstandingAdapter:
    """Create a video understanding adapter without initializing real clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.video_provider == "ark":
        from assistant_agent.providers.ark_video_understanding import (
            ArkVideoUnderstandingAdapter,
            ArkVideoUnderstandingConfig,
        )

        return ArkVideoUnderstandingAdapter(
            ArkVideoUnderstandingConfig(
                api_key=resolved.video_understanding_api_key,
                base_url=resolved.video_understanding_base_url or "https://ark.cn-beijing.volces.com/api/v3",
                model=resolved.video_understanding_model,
            )
        )
    if resolved.video_provider == "qwen":
        from assistant_agent.providers.qwen_video_understanding import QwenVideoUnderstandingAdapter
        from assistant_agent.video_ai.qwen.vision_client import QwenVLConfig

        return QwenVideoUnderstandingAdapter(
            QwenVLConfig(
                api_key=resolved.video_understanding_api_key,
                base_url=(
                    resolved.video_understanding_base_url
                    or "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                model=resolved.video_understanding_model,
                timeout_seconds=resolved.video_understanding_timeout_seconds,
            )
        )
    if resolved.video_provider == "http":
        return HttpVideoUnderstandingAdapter(
            base_url=resolved.video_understanding_base_url,
            api_key=resolved.video_understanding_api_key,
            model=resolved.video_understanding_model,
            timeout_seconds=resolved.video_understanding_timeout_seconds,
            max_video_bytes=resolved.max_video_bytes,
            max_video_seconds=resolved.max_video_seconds,
        )
    return MockVideoUnderstandingAdapter()


def create_realtime_video_understanding_adapter(
    config: ProviderConfig | None = None,
) -> VideoUnderstandingAdapter:
    """Select Qwen realtime only for background live-video observations."""

    resolved = config or ProviderConfig.from_env()
    if resolved.vision_provider == "qwen":
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
    return create_video_understanding_adapter(resolved)


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


def _number_metadata(request: VideoUnderstandingRequest, *keys: str) -> float | None:
    for key in keys:
        value = request.metadata.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _failed_result(
    *,
    provider: str,
    model: str | None,
    code: str,
    message: str,
    recoverable: bool,
) -> VideoUnderstandingResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability="video_understanding",
    )
    return VideoUnderstandingResult(
        summary=error.message,
        provider=provider,
        model=model,
        output_ref=f"provider://video/{provider}/failed",
        errors=[{"code": error.code, "message": error.message, "recoverable": error.recoverable}],
    )
