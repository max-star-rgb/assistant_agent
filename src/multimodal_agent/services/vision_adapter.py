"""Vision understanding adapter interface and mock implementation."""

from typing import Protocol

from pydantic import BaseModel, Field

from multimodal_agent.schemas.perception import VisualUnderstandingResult


class VisionUnderstandingInput(BaseModel):
    """Input for image or video understanding."""

    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    question: str | None = None


class VisionUnderstandingAdapter(Protocol):
    """Adapter contract for VLM or Video MLLM providers."""

    def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
        """Return structured visual understanding."""


class MockVisionUnderstandingAdapter:
    """Deterministic local adapter for tests and MVP flows."""

    def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
        if not input.image_ids and not input.video_ids:
            raise ValueError("缺少图片或视频 ID，无法进行视觉理解")

        if input.video_ids:
            return VisualUnderstandingResult(
                objects=["白色低帮运动鞋"],
                colors=["白色"],
                materials=["皮革", "橡胶"],
                scene="室内展示场景",
                style_tags=["简约", "日系"],
                text_in_media=[],
                summary="视频中展示了一双白色低帮运动鞋，整体为简约日系风格。",
            )

        return VisualUnderstandingResult(
            objects=["白色低帮运动鞋"],
            colors=["白色"],
            materials=["皮革", "橡胶"],
            scene="室内展示场景",
            style_tags=["简约", "日系"],
            text_in_media=[],
            summary="图片中展示了一双白色低帮运动鞋，整体为简约日系风格。",
        )
