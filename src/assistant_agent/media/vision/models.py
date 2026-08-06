"""Perception and visual understanding schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field


MAX_VISUAL_GROUNDING_ITEMS = 20


class VisualUnderstandingResult(BaseModel):
    """Structured result from image or video understanding."""

    objects: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    scene: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    text_in_media: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class VisionUnderstandingRequest(BaseModel):
    """图片、显式视频和实时关键帧理解的统一请求。"""

    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    video_ref: str | None = None
    frame_refs: list[str] = Field(default_factory=list)
    context_id: str | None = None
    question: str | None = Field(
        default=None,
        description="希望重点从当前图片或视频中回答的问题；无特定重点时省略。",
    )
    user_query: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    max_frames: int | None = Field(default=None, ge=1)
    sample_strategy: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_context: str | list[str] | None = None


class LiveViewInspectRequest(VisionUnderstandingRequest):
    """Model query plus runtime-owned references for one live-view inspection."""

    query: str = Field(
        min_length=1,
        max_length=500,
        description="需要根据当前实时画面回答的具体问题。",
    )


class VisionUnderstandingResult(BaseModel):
    """Unified result whose scene/object/action fields describe current visual facts."""

    summary: str = Field(min_length=1)
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    changes: list[str] = Field(
        default_factory=list, max_length=MAX_VISUAL_GROUNDING_ITEMS
    )
    uncertainties: list[str] = Field(
        default_factory=list, max_length=MAX_VISUAL_GROUNDING_ITEMS
    )
    scene: str | None = None
    products: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    text_in_media: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    timestamps: list[dict[str, Any]] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str = "mock"
    model: str | None = None
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    source: str | None = None
    media_kind: Literal["image", "explicit_video", "live_view"] | None = None
    media_refs: list[str] = Field(default_factory=list)


class PerceptionBundle(BaseModel):
    """All perception outputs available to the agent for a request."""

    visual: VisualUnderstandingResult | None = None
    asr_text: str | None = None
    ocr_text: list[str] = Field(default_factory=list)
    text_summary: str | None = None


class VideoUnderstandingRequest(BaseModel):
    """Structured request for video understanding providers."""

    video_ref: str | None = None
    video_ids: list[str] = Field(default_factory=list)
    frame_refs: list[str] = Field(default_factory=list)
    context_id: str | None = None
    user_query: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    max_frames: int | None = Field(default=None, ge=1)
    sample_strategy: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_context: str | list[str] | None = None


class VideoUnderstandingResult(BaseModel):
    """Video result whose scene/object/action fields describe current-frame facts."""

    summary: str = Field(min_length=1)
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    changes: list[str] = Field(
        default_factory=list, max_length=MAX_VISUAL_GROUNDING_ITEMS
    )
    uncertainties: list[str] = Field(
        default_factory=list, max_length=MAX_VISUAL_GROUNDING_ITEMS
    )
    scene: str | None = None
    products: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    timestamps: list[dict[str, Any]] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str = "mock"
    model: str | None = None
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    source: str | None = None
    media_kind: Literal["explicit_video", "live_view"] | None = None
    media_refs: list[str] = Field(default_factory=list)
