"""Vision understanding tool backed by an adapter."""

from multimodal_agent.schemas.perception import VisualUnderstandingResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
    VisionUnderstandingInput,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class VisionUnderstandingTool(MockTool):
    name = "vision_understanding"
    description = "Image and video understanding through a vision adapter."
    input_schema = VisionUnderstandingInput
    output_schema = VisualUnderstandingResult

    def __init__(self, adapter: VisionUnderstandingAdapter | None = None) -> None:
        self.adapter = adapter or MockVisionUnderstandingAdapter()

    def _run(self, input: VisionUnderstandingInput, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.understand(input)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=result.model_dump(),
            output_ref=_vision_output_ref(self.adapter),
            latency_ms=1,
        )


def _vision_output_ref(adapter: VisionUnderstandingAdapter) -> str:
    config = getattr(adapter, "config", None)
    provider = getattr(config, "provider", None)
    if provider:
        return f"provider://vision/{provider}"
    return "mock://vision/white-low-top-sneaker"
