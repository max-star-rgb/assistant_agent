"""Direct background VLM service for selected realtime keyframes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    frame_refs: tuple[str, ...] = Field(min_length=1, max_length=5)
    frame_sequences: tuple[int, ...] = Field(min_length=1, max_length=5)
    frame_timestamps_ms: tuple[int | None, ...] = Field(min_length=1, max_length=5)
    visual_window_id: str | None = Field(default=None, min_length=1, max_length=160)
    window_start_sequence: int | None = Field(default=None, ge=0)
    target_sequence: int | None = Field(default=None, ge=0)
    window_role: Literal["target", "context", "background"] = "background"
    provider_connection_isolated: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> "RealtimeVisualObservationRequest":
        if not (
            len(self.frame_refs)
            == len(self.frame_sequences)
            == len(self.frame_timestamps_ms)
        ):
            raise ValueError("realtime visual window fields must have equal lengths")
        if any(
            current >= following
            for current, following in zip(
                self.frame_sequences,
                self.frame_sequences[1:],
            )
        ):
            raise ValueError("realtime visual frame sequences must be increasing")
        if self.target_sequence is not None and self.target_sequence != self.frame_sequence:
            raise ValueError("realtime visual target must be the last supplied keyframe")
        return self

    @property
    def frame_ref(self) -> str:
        return self.frame_refs[-1]

    @property
    def frame_sequence(self) -> int:
        return self.frame_sequences[-1]

    @property
    def frame_timestamp_ms(self) -> int | None:
        return self.frame_timestamps_ms[-1]


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
            frame_refs=list(request.frame_refs),
            user_query=(
                "按时间顺序理解这些关键帧。最后一张是目标当前画面，前面的帧只用于理解变化过程；"
                "summary 必须优先、明确描述最后一张画面，可以补充与前序关键帧相比发生的变化。"
            ),
            user_id=request.user_id,
            session_id=request.session_id,
            metadata={
                "frame_sequence": request.frame_sequence,
                "frame_sequences": list(request.frame_sequences),
                "frame_timestamp_ms": request.frame_timestamp_ms,
                "_force_video_understanding": True,
                "visual_context_compaction": {
                    "status": "disabled_keyframe_window_text",
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
                media_count=len(request.frame_refs),
                frame_sequence=request.frame_sequence,
                visual_window_id=request.visual_window_id,
                window_start_sequence=request.window_start_sequence,
                target_sequence=request.target_sequence,
                window_role=request.window_role,
                provider_connection_isolated=(
                    request.provider_connection_isolated
                ),
                prompt_version="realtime-keyframe-window-v1",
                local_input_content={
                    "mode": "background_keyframe_observation",
                    "media_kind": "live_view",
                    "frame_sequence": request.frame_sequence,
                    "frame_sequences": list(request.frame_sequences),
                    "visual_window_id": request.visual_window_id,
                    "window_start_sequence": request.window_start_sequence,
                    "target_sequence": request.target_sequence,
                    "window_role": request.window_role,
                    "provider_connection_isolated": (
                        request.provider_connection_isolated
                    ),
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
