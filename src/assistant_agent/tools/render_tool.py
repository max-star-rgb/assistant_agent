"""3D render tool backed by an adapter."""

from typing import Any

from assistant_agent.schemas.generation import RenderResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.services.render_adapter import (
    RenderAdapter,
    RenderRequest,
    create_render_adapter,
)
from assistant_agent.services.tool_manifest import RENDER_3D_CAPABILITY, RENDER_3D_TOOL_NAME
from assistant_agent.tools.base import MockTool, ToolContext


class Render3DTool(MockTool):
    name = RENDER_3D_TOOL_NAME
    description = "3D and scene rendering through an adapter."
    input_schema = RenderRequest
    output_schema = RenderResult
    category = "generate"
    requires_confirmation = False

    def __init__(self, adapter: RenderAdapter | None = None) -> None:
        self.adapter = adapter or create_render_adapter()

    def _run(self, input: RenderRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.render(input)
        success = result.status == "succeeded"
        error = None
        if not success:
            error = result.error or (
                result.errors[0]["message"] if result.errors else None
            )
        data = result.model_dump(mode="json")
        output_ref = result.output_ref or result.preview_url
        contract = build_capability_output_contract(
            capability=RENDER_3D_CAPABILITY,
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
            model_observation=_render_model_observation(data, output_ref=output_ref),
            error=error,
            output_ref=output_ref,
            latency_ms=result.latency_ms or 1,
            contract=contract,
        )


def _render_model_observation(
    data: dict[str, Any], *, output_ref: str | None
) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": data.get("status"),
        "summary": _render_summary(data),
        "preview_url": data.get("preview_url"),
        "image_url": data.get("image_url"),
        "video_url": data.get("video_url"),
        "model_url": data.get("model_url"),
        "output_ref": output_ref,
        "scene_description": data.get("scene_description"),
        "errors": data.get("errors"),
    }
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _render_summary(data: dict[str, Any]) -> str:
    if data.get("status") == "succeeded":
        return "3D render succeeded."
    if data.get("error"):
        return str(data["error"])
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "3D render failed.")
    return "3D render failed."
