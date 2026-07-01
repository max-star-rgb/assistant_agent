"""Video understanding tool backed by a video adapter."""

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.video_adapter import (
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from assistant_agent.services.video_context import DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE, VideoContextStore
from assistant_agent.tools.base import MockTool, ToolContext


class VideoUnderstandingTool(MockTool):
    name = "video_understanding"
    description = "Video understanding through a Video MLLM adapter."
    input_schema = VideoUnderstandingRequest
    output_schema = VideoUnderstandingResult

    def __init__(
        self,
        adapter: VideoUnderstandingAdapter | None = None,
        *,
        context_store: VideoContextStore | None = None,
        context_window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    ) -> None:
        self.adapter = adapter or create_video_understanding_adapter()
        self.context_store = context_store
        self.context_window_size = context_window_size

    def _run(self, input: VideoUnderstandingRequest, context: ToolContext) -> ToolResult:
        input = self._with_context_frames(input)
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

    def _with_context_frames(self, input: VideoUnderstandingRequest) -> VideoUnderstandingRequest:
        video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
        if not video_ref:
            return input
        if input.frame_refs or self.context_store is None:
            return input.model_copy(update={"video_ref": video_ref})
        limit = input.max_frames or self.context_window_size
        frames = self.context_store.get_recent_frames(video_ref, limit=limit)
        if not frames:
            return input.model_copy(update={"video_ref": video_ref})
        metadata = {
            **input.metadata,
            "context_window_size": self.context_window_size,
            "context_frame_count": len(frames),
            "context_frame_ids": [frame.frame_id for frame in frames],
        }
        return input.model_copy(
            update={
                "video_ref": video_ref,
                "context_id": video_ref,
                "frame_refs": [frame.uri for frame in frames],
                "metadata": metadata,
            }
        )


def _error_code(message: str) -> str:
    if ":" in message:
        return message.split(":", maxsplit=1)[0]
    return "video_understanding_failed"
