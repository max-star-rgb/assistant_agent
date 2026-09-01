"""Unified vision understanding client abstraction."""

from __future__ import annotations

from time import perf_counter
from typing import Protocol

from langchain_core.runnables import RunnableConfig
from assistant_agent.config import ProviderConfig
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
    VisualUnderstandingResult,
)
from assistant_agent.providers.provider_selection import create_vision_adapter
from assistant_agent.media.video.video_adapter import (
    MockVideoUnderstandingAdapter,
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from assistant_agent.media.vision.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
    VisionUnderstandingInput,
)


class VisionUnderstandingClient(Protocol):
    """Unified client contract for image, explicit video, and realtime keyframes."""

    def understand(
        self,
        request: VisionUnderstandingRequest,
        *,
        config: RunnableConfig | None = None,
    ) -> VisionUnderstandingResult:
        """Return a provider-neutral structured vision result."""


class VisionClient(VisionUnderstandingClient, Protocol):
    """Short alias used by callers that do not need the longer historical name."""


class AdapterVisionUnderstandingClient:
    """Dispatch unified vision requests to existing image and video adapters."""

    def __init__(
        self,
        *,
        image_adapter: VisionUnderstandingAdapter | None = None,
        video_adapter: VideoUnderstandingAdapter | None = None,
    ) -> None:
        self.image_adapter = image_adapter or MockVisionUnderstandingAdapter()
        self.video_adapter = video_adapter or MockVideoUnderstandingAdapter()

    def understand(
        self,
        request: VisionUnderstandingRequest,
        *,
        config: RunnableConfig | None = None,
    ) -> VisionUnderstandingResult:
        if vision_request_has_video(request):
            result = self.video_adapter.understand_video(
                video_request_from_vision_request(request)
            )
            return vision_result_from_video_result(result).model_copy(
                update={
                    "source": result.source or "explicit_video",
                    "media_kind": result.media_kind or "explicit_video",
                    "media_refs": (
                        list(result.media_refs)
                        or list(request.video_ids)
                        or ([request.video_ref] if request.video_ref else [])
                    ),
                }
            )
        started_at = perf_counter()
        result = self.image_adapter.understand(
            image_input_from_vision_request(request), config=config
        )
        latency_ms = max(0, int((perf_counter() - started_at) * 1000))
        return vision_result_from_visual_result(
            result,
            provider=_adapter_provider(self.image_adapter),
            model=_adapter_model(self.image_adapter),
            output_ref=_vision_output_ref(self.image_adapter),
            latency_ms=latency_ms,
            source="request_image",
            media_refs=list(request.image_ids),
        )

    @property
    def last_observation_diagnostics(self) -> dict[str, object] | None:
        diagnostics = getattr(self.video_adapter, "last_observation_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else None

    def close(self) -> None:
        for adapter in (self.video_adapter, self.image_adapter):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()


class MockVisionUnderstandingClient(AdapterVisionUnderstandingClient):
    """Deterministic unified client for offline tests and local demo flows."""

    def __init__(self) -> None:
        super().__init__(
            image_adapter=MockVisionUnderstandingAdapter(),
            video_adapter=MockVideoUnderstandingAdapter(),
        )


def create_vision_understanding_client(
    config: ProviderConfig | None = None,
) -> VisionUnderstandingClient:
    """Create the configured unified vision client."""

    resolved = config or ProviderConfig.from_env()
    if resolved.provider_mode == "real" and resolved.vision_provider == "mock":
        raise ValueError("real provider mode requires a configured vision provider")
    return AdapterVisionUnderstandingClient(
        image_adapter=create_vision_adapter(resolved),
        video_adapter=create_video_understanding_adapter(resolved),
    )


def vision_request_has_video(request: VisionUnderstandingRequest) -> bool:
    """Return whether the unified request should use video understanding."""

    return bool(
        request.video_ref
        or request.video_ids
        or request.frame_refs
        or request.metadata.get("_force_video_understanding") is True
    )


def vision_request_from_video_request(
    request: VideoUnderstandingRequest,
) -> VisionUnderstandingRequest:
    """Convert the legacy video request model into the unified request model."""

    return VisionUnderstandingRequest(
        video_ref=request.video_ref,
        video_ids=list(request.video_ids),
        frame_refs=list(request.frame_refs),
        context_id=request.context_id,
        user_query=request.user_query,
        user_id=request.user_id,
        session_id=request.session_id,
        max_frames=request.max_frames,
        sample_strategy=request.sample_strategy,
        metadata=_video_request_metadata(request),
        memory_context=request.memory_context,
    )


def video_request_from_vision_request(
    request: VisionUnderstandingRequest,
) -> VideoUnderstandingRequest:
    """Convert a unified request into the legacy video provider request model."""

    video_ref = request.video_ref or (request.video_ids[0] if request.video_ids else None)
    return VideoUnderstandingRequest(
        video_ref=video_ref,
        video_ids=list(request.video_ids),
        frame_refs=list(request.frame_refs),
        context_id=request.context_id,
        user_query=request.user_query or request.question,
        user_id=request.user_id,
        session_id=request.session_id,
        max_frames=request.max_frames,
        sample_strategy=request.sample_strategy,
        metadata=dict(request.metadata),
        memory_context=request.memory_context,
    )


def image_input_from_vision_request(
    request: VisionUnderstandingRequest,
) -> VisionUnderstandingInput:
    """Convert unified image requests to the legacy image adapter input."""

    return VisionUnderstandingInput(
        image_ids=list(request.image_ids),
        video_ids=list(request.video_ids),
        question=request.question or request.user_query,
    )


def vision_result_from_visual_result(
    result: VisualUnderstandingResult,
    *,
    provider: str,
    model: str | None,
    output_ref: str,
    latency_ms: int | None,
    source: str | None = None,
    media_refs: list[str] | None = None,
) -> VisionUnderstandingResult:
    """Map the legacy image result into the unified result schema."""

    return VisionUnderstandingResult(
        summary=result.summary,
        objects=list(result.objects),
        colors=list(result.colors),
        materials=list(result.materials),
        scene=result.scene,
        style_tags=list(result.style_tags),
        text_in_media=list(result.text_in_media),
        provider=provider,
        model=model,
        output_ref=output_ref,
        errors=[],
        latency_ms=latency_ms,
        source=source,
        media_kind="image",
        media_refs=list(media_refs or ()),
    )


def vision_result_from_video_result(
    result: VideoUnderstandingResult,
) -> VisionUnderstandingResult:
    """Map the legacy video provider result into the unified result schema."""

    return VisionUnderstandingResult(
        summary=result.summary,
        objects=list(result.objects),
        people=list(result.people),
        actions=list(result.actions),
        events=list(result.events),
        changes=list(result.changes),
        uncertainties=list(result.uncertainties),
        scene=result.scene,
        products=list(result.products),
        brands=list(result.brands),
        colors=list(result.colors),
        materials=list(result.materials),
        text_in_media=list(result.text_in_video),
        text_in_video=list(result.text_in_video),
        timestamps=[dict(item) for item in result.timestamps],
        style_tags=list(result.style_tags),
        confidence=result.confidence,
        provider=result.provider,
        model=result.model,
        output_ref=result.output_ref,
        errors=[dict(item) for item in result.errors],
        latency_ms=result.latency_ms,
        source=result.source,
        media_kind=result.media_kind,
        media_refs=list(result.media_refs),
    )


def video_result_from_vision_result(
    result: VisionUnderstandingResult,
) -> VideoUnderstandingResult:
    """Map unified vision output back to the legacy video tool payload shape."""

    return VideoUnderstandingResult(
        summary=result.summary,
        objects=list(result.objects),
        people=list(result.people),
        actions=list(result.actions),
        events=list(result.events),
        changes=list(result.changes),
        uncertainties=list(result.uncertainties),
        scene=result.scene,
        products=list(result.products),
        brands=list(result.brands),
        colors=list(result.colors),
        materials=list(result.materials),
        text_in_video=list(result.text_in_video or result.text_in_media),
        timestamps=[dict(item) for item in result.timestamps],
        style_tags=list(result.style_tags),
        confidence=result.confidence,
        provider=result.provider,
        model=result.model,
        output_ref=result.output_ref,
        errors=[dict(item) for item in result.errors],
        latency_ms=result.latency_ms,
        source=result.source,
        media_kind=result.media_kind,
        media_refs=list(result.media_refs),
    )


def _adapter_provider(adapter: object) -> str:
    config = getattr(adapter, "config", None)
    provider = getattr(config, "provider", None)
    if provider:
        return str(provider)
    provider_attr = getattr(adapter, "provider", None)
    return str(provider_attr) if provider_attr else "mock"


def _adapter_model(adapter: object | None) -> str | None:
    config = getattr(adapter, "config", None)
    model = getattr(config, "model", None)
    return str(model) if model else None


def _vision_output_ref(adapter: object) -> str:
    provider = _adapter_provider(adapter)
    if provider and provider != "mock":
        return f"provider://vision/{provider}"
    return "mock://vision/white-low-top-sneaker"


def _video_request_metadata(request: VideoUnderstandingRequest) -> dict[str, object]:
    metadata = dict(request.metadata)
    if not request.video_ref and not request.video_ids and not request.frame_refs:
        metadata["_force_video_understanding"] = True
    return metadata
