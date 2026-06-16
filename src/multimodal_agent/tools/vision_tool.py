"""Vision understanding tool backed by an adapter."""

from multimodal_agent.schemas.perception import VisualUnderstandingResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
    VisionUnderstandingInput,
)
from multimodal_agent.services.provider_errors import ProviderAdapterError, build_provider_error
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
        except ProviderAdapterError as exc:
            capability = "video_understanding" if input.video_ids else "image_understanding"
            provider = getattr(getattr(self.adapter, "config", None), "provider", "unknown")
            error = build_provider_error(exc.code, exc.message, provider=provider, capability=capability)
            contract = build_capability_output_contract(
                capability=capability,
                status="failed",
                errors=[error.model_dump(mode="json")],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"{error.code}: {error.message}",
                contract=contract,
            )
        except ValueError as exc:
            message = build_provider_error("provider_request_invalid", str(exc), recoverable=True).message
            contract = build_capability_output_contract(
                capability="video_understanding" if input.video_ids else "image_understanding",
                status="failed",
                errors=[{"code": "missing_required_input", "message": message, "recoverable": True}],
            )
            return ToolResult(tool_name=self.name, success=False, error=message, contract=contract)

        output_ref = _vision_output_ref(self.adapter)
        capability = "video_understanding" if input.video_ids else "image_understanding"
        contract = build_capability_output_contract(
            capability=capability,
            status="succeeded",
            output_ref=output_ref,
            data=result.model_dump(),
            metadata={"provider": getattr(getattr(self.adapter, "config", None), "provider", "mock"), "latency_ms": 1},
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=result.model_dump(),
            output_ref=output_ref,
            latency_ms=1,
            contract=contract,
        )


def _vision_output_ref(adapter: VisionUnderstandingAdapter) -> str:
    config = getattr(adapter, "config", None)
    provider = getattr(config, "provider", None)
    if provider:
        return f"provider://vision/{provider}"
    return "mock://vision/white-low-top-sneaker"
