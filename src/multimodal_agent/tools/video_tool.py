"""Video understanding tool backed by a video adapter."""

from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.video_adapter import (
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class VideoUnderstandingTool(MockTool):
    name = "video_understanding"
    description = "Video understanding through a Video MLLM adapter."
    input_schema = VideoUnderstandingRequest
    output_schema = VideoUnderstandingResult

    def __init__(self, adapter: VideoUnderstandingAdapter | None = None) -> None:
        self.adapter = adapter or create_video_understanding_adapter()

    def _run(self, input: VideoUnderstandingRequest, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.understand_video(input)
        except ValueError as exc:
            contract = build_capability_output_contract(
                capability="video_understanding",
                status="failed",
                errors=[{"code": _error_code(str(exc)), "message": str(exc), "recoverable": True}],
            )
            return ToolResult(tool_name=self.name, success=False, error=str(exc), contract=contract)

        payload = result.model_dump(mode="json")
        output_ref = result.output_ref
        status = "failed" if result.errors else "succeeded"
        contract = build_capability_output_contract(
            capability="video_understanding",
            status=status,
            output_ref=output_ref,
            data=payload,
            errors=result.errors,
            metadata={
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=not result.errors,
            data=payload,
            error=result.errors[0]["message"] if result.errors else None,
            output_ref=output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _error_code(message: str) -> str:
    if ":" in message:
        return message.split(":", maxsplit=1)[0]
    return "video_understanding_failed"
