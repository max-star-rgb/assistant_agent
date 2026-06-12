"""Image generation tool backed by an adapter."""

from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.image_generation_adapter import (
    ImageGenerationAdapter,
    ImageGenerationInput,
    MockImageGenerationAdapter,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class ImageGenerationTool(MockTool):
    name = "image_generation"
    description = "Image generation through an adapter."
    input_schema = ImageGenerationInput
    output_schema = ImageGenerationResult

    def __init__(self, adapter: ImageGenerationAdapter | None = None) -> None:
        self.adapter = adapter or MockImageGenerationAdapter()

    def _run(self, input: ImageGenerationInput, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.generate(input)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=result.model_dump(),
            output_ref=result.image_url,
            latency_ms=1,
        )
