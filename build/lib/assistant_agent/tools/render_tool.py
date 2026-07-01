"""3D render tool backed by an adapter."""

from assistant_agent.schemas.generation import RenderResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.services.render_adapter import RenderAdapter, RenderRequest, create_render_adapter
from assistant_agent.tools.base import MockTool, ToolContext


class Render3DTool(MockTool):
    name = "render_3d"
    description = "3D and scene rendering through an adapter."
    input_schema = RenderRequest
    output_schema = RenderResult

    def __init__(self, adapter: RenderAdapter | None = None) -> None:
        self.adapter = adapter or create_render_adapter()

    def _run(self, input: RenderRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.render(input)
        success = result.status == "succeeded"
        error = None
        if not success:
            error = result.error or (result.errors[0]["message"] if result.errors else None)
        data = result.model_dump(mode="json")
        output_ref = result.output_ref or result.preview_url
        contract = build_capability_output_contract(
            capability="render_3d",
            status="succeeded" if success else "failed",
            output_ref=output_ref,
            data={
                "preview_url": result.preview_url,
                "model_url": result.model_url,
                "scene_description": result.scene_description,
            },
            errors=result.errors,
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )

        return ToolResult(
            tool_name=self.name,
            success=success,
            data={**data, "contract": contract.model_dump(mode="json")},
            error=error,
            output_ref=output_ref,
            latency_ms=result.latency_ms or 1,
            contract=contract,
        )
