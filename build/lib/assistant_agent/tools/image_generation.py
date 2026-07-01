"""Public image generation tool schemas and tool class."""

from pydantic import BaseModel, Field

from assistant_agent.schemas.generation import ImageGenerationInput
from assistant_agent.tools.image_generation_tool import ImageGenerationTool


class ImageGenerationOutput(BaseModel):
    """Public output shape for image generation tools."""

    image_urls: list[str] = Field(default_factory=list)
    request_id: str | None = None


__all__ = ["ImageGenerationInput", "ImageGenerationOutput", "ImageGenerationTool"]
