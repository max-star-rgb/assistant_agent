"""3D render tool backed by an adapter."""

from multimodal_agent.schemas.generation import RenderResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.render_adapter import MockRenderAdapter, RenderAdapter, RenderInput
from multimodal_agent.tools.base import MockTool, ToolContext


class Render3DTool(MockTool):
    name = "render_3d"
    description = "3D and scene rendering through an adapter."
    input_schema = RenderInput
    output_schema = RenderResult

    def __init__(self, adapter: RenderAdapter | None = None) -> None:
        self.adapter = adapter or MockRenderAdapter()

    def _run(self, input: RenderInput, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.create_render(input)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=result.model_dump(),
            output_ref=result.preview_url,
            latency_ms=1,
        )
