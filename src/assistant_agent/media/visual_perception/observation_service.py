"""Direct background VLM service for selected realtime keyframes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.vision.models import (
    VideoUnderstandingResult,
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.media.vision.observability import (
    VisionInferenceTraceContext,
    VisionInferenceTraceLink,
    observe_vision_inference,
)
from assistant_agent.media.vision.vision_client import (
    VisionUnderstandingClient,
    video_result_from_vision_result,
)


class RealtimeVisualObservationRequest(BaseModel):
    """Trusted frame facts supplied by the realtime perception pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: str = Field(min_length=1, max_length=320)
    session_id: str = Field(min_length=1, max_length=320)
    video_id: str = Field(min_length=1, max_length=500)
    frame_ref: str = Field(min_length=1, max_length=4_000)
    frame_sequence: int = Field(ge=0)
    frame_timestamp_ms: int | None = Field(default=None, ge=0)


class RealtimeVisualObservationOutcome(BaseModel):
    """Provider-neutral outcome consumed by the background observer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result: VideoUnderstandingResult | None = None
    error: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    trace_link: VisionInferenceTraceLink | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and not self.result.errors


class RealtimeVisualObservationService:
    """Call the realtime VLM directly without constructing Agent runtime state."""

    def __init__(self, *, client: VisionUnderstandingClient) -> None:
        self._client = client
        self._closed = False

    def observe(
        self,
        request: RealtimeVisualObservationRequest,
        *,
        trace_context: VisionInferenceTraceContext | None = None,
    ) -> RealtimeVisualObservationOutcome:
        if self._closed:
            raise RuntimeError("realtime_visual_observation_service_closed")
        provider_request = VisionUnderstandingRequest(
            video_ref=request.video_id,
            frame_refs=[request.frame_ref],
            user_query="简短描述当前单帧中直接可见的内容。",
            user_id=request.user_id,
            session_id=request.session_id,
            metadata={
                "frame_sequence": request.frame_sequence,
                "frame_timestamp_ms": request.frame_timestamp_ms,
                "_force_video_understanding": True,
                "visual_context_compaction": {
                    "status": "disabled_single_frame_text",
                    "compacted": False,
                },
            },
        )
        trace_links: list[VisionInferenceTraceLink] = []

        def call() -> VisionUnderstandingResult:
            return self._client.understand(provider_request)

        raw = (
            observe_vision_inference(
                call,
                context=trace_context,
                capability="video_understanding",
                source="background_keyframe_observation",
                media_kind="live_view",
                media_count=1,
                frame_sequence=request.frame_sequence,
                prompt_version="realtime-single-frame-v1",
                local_input_content={
                    "mode": "background_keyframe_observation",
                    "media_kind": "live_view",
                    "frame_sequence": request.frame_sequence,
                    "query": provider_request.user_query,
                },
                trace_link_callback=trace_links.append,
            )
            if trace_context is not None
            else call()
        )
        result = video_result_from_vision_result(
            raw.model_copy(
                update={
                    "source": "background_keyframe_observation",
                    "media_kind": "live_view",
                    "media_refs": [request.video_id],
                }
            )
        )
        error = dict(result.errors[0]) if result.errors else None
        return RealtimeVisualObservationOutcome(
            result=result if not result.errors else None,
            error=error,
            diagnostics=_client_diagnostics(self._client),
            trace_link=trace_links[-1] if trace_links else None,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


RealtimeVisualObservationServiceFactory = Callable[
    [],
    RealtimeVisualObservationService,
]


def _client_diagnostics(client: VisionUnderstandingClient) -> dict[str, Any]:
    diagnostics = getattr(client, "last_observation_diagnostics", None)
    if isinstance(diagnostics, Mapping):
        return dict(diagnostics)
    return {}


__all__ = [
    "RealtimeVisualObservationOutcome",
    "RealtimeVisualObservationRequest",
    "RealtimeVisualObservationService",
    "RealtimeVisualObservationServiceFactory",
]
