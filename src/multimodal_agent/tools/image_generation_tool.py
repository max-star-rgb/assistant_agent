"""Image generation tool backed by an adapter."""

from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.agent.prompt_builder import build_text_capability_output
from multimodal_agent.schemas.capability_output import CapabilityOutputContract
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
            data, contract = _image_generation_error_contract(str(exc))
            return ToolResult(tool_name=self.name, success=False, error=str(exc), data=data, contract=contract)
        data, contract = _image_generation_output_contract(result)
        if result.status == "failed":
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                error=result.error or "image_generation_failed",
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            output_ref=result.output_ref or result.image_url,
            latency_ms=result.latency_ms or 1,
            contract=contract,
        )


def _image_generation_output_contract(result: ImageGenerationResult) -> tuple[dict, CapabilityOutputContract]:
    data = {
        "task_id": result.task_id,
        "image_url": result.image_url,
        "prompt": result.prompt,
        "prompt_used": result.prompt_used or result.prompt,
        "provider": result.provider,
        "model": result.model,
    }
    public = build_text_capability_output(
        capability="image_generation",
        status=result.status,
        output_ref=result.output_ref or result.image_url,
        data=data,
        errors=result.errors,
    )
    payload = {
        **data,
        "status": result.status,
        "output_ref": result.output_ref or result.image_url,
        "errors": result.errors,
        "contract": public,
    }
    return payload, CapabilityOutputContract.model_validate(public)


def _image_generation_error_contract(message: str) -> tuple[dict, CapabilityOutputContract]:
    public = build_text_capability_output(
        capability="image_generation",
        status="failed",
        errors=[{"code": "missing_required_input", "message": message, "recoverable": True}],
    )
    return {"status": "failed", "errors": public["errors"], "contract": public}, CapabilityOutputContract.model_validate(public)
