"""Vision understanding tool backed by an adapter."""

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.perception import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.services.video_adapter import VideoUnderstandingAdapter
from assistant_agent.services.video_context import (
    DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    VideoContextStore,
)
from assistant_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
)
from assistant_agent.services.vision_client import (
    AdapterVisionUnderstandingClient,
    VisionUnderstandingClient,
    video_request_from_vision_request,
    vision_request_has_video,
)
from assistant_agent.services.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.schemas.tool_ids import IMAGE_UNDERSTANDING_CAPABILITY, IMAGE_UNDERSTANDING_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import ToolInputBinding
from assistant_agent.tools.plugins.vision.video_branch import VideoUnderstandingBranch


class VisionUnderstandingTool(ToolBase):
    name = IMAGE_UNDERSTANDING_TOOL_NAME
    description = (
        "理解当前请求附带的图片或视频。可提供聚焦问题；"
        "运行时会选择对应媒体和内部视觉分支。"
    )
    input_schema = VisionUnderstandingRequest
    output_schema = VisionUnderstandingResult
    category = "read"
    toolset = "vision"
    requires_confirmation = False
    defer_loading = False
    requires_media = ["image", "video"]
    input_bindings = (
        ToolInputBinding(field="image_ids", source="request", key="image_ids"),
        ToolInputBinding(field="video_ids", source="request", key="video_ids"),
        ToolInputBinding(field="video_ref", source="constant", value=None),
        ToolInputBinding(field="frame_refs", source="constant", value=[]),
        ToolInputBinding(field="context_id", source="constant", value=None),
        ToolInputBinding(field="user_query", source="request", key="text"),
        ToolInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        ToolInputBinding(field="session_id", source="runtime_identity", key="session_id"),
        ToolInputBinding(field="max_frames", source="constant", value=None),
        ToolInputBinding(field="sample_strategy", source="constant", value=None),
        ToolInputBinding(field="metadata", source="constant", value={}),
        ToolInputBinding(field="memory_context", source="memory_context", key="text"),
    )

    def __init__(
        self,
        adapter: VisionUnderstandingAdapter | None = None,
        *,
        client: VisionUnderstandingClient | None = None,
        video_adapter: VideoUnderstandingAdapter | None = None,
        context_store: VideoContextStore | None = None,
        memory_store: RealtimeVideoMemoryStore | None = None,
        context_window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    ) -> None:
        self.adapter = (
            adapter
            or getattr(client, "image_adapter", None)
            or MockVisionUnderstandingAdapter()
        )
        self.client = client or AdapterVisionUnderstandingClient(
            image_adapter=self.adapter,
            video_adapter=video_adapter,
        )
        self._video_branch = VideoUnderstandingBranch(
            client=self.client,
            adapter=video_adapter,
            context_store=context_store,
            memory_store=memory_store,
            context_window_size=context_window_size,
        )

    @property
    def video_adapter(self) -> VideoUnderstandingAdapter:
        return self._video_branch.adapter

    @property
    def memory_store(self) -> RealtimeVideoMemoryStore | None:
        return self._video_branch.memory_store

    @memory_store.setter
    def memory_store(self, value: RealtimeVideoMemoryStore | None) -> None:
        self._video_branch.memory_store = value

    def _run(self, input: VisionUnderstandingRequest, context: ToolContext) -> ToolResult:
        if vision_request_has_video(input):
            result = self._video_branch.run(
                video_request_from_vision_request(input), context
            )
            return result.model_copy(update={"tool_name": self.name})
        try:
            result = self.client.understand(input)
        except ProviderAdapterError as exc:
            capability = IMAGE_UNDERSTANDING_CAPABILITY
            provider = getattr(
                getattr(self.adapter, "config", None), "provider", "unknown"
            )
            error = build_provider_error(
                exc.code, exc.message, provider=provider, capability=capability
            )
            contract = build_capability_output_contract(
                capability=capability,
                status="failed",
                errors=[error.model_dump(mode="json")],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                model_observation=_vision_error_model_observation(
                    error.model_dump(mode="json")
                ),
                error=f"{error.code}: {error.message}",
                contract=contract,
            )
        except ValueError as exc:
            message = build_provider_error(
                "provider_request_invalid", str(exc), recoverable=True
            ).message
            contract = build_capability_output_contract(
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
                status="failed",
                errors=[
                    {
                        "code": "missing_required_input",
                        "message": message,
                        "recoverable": True,
                    }
                ],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                model_observation={
                    "summary": message,
                    "errors": [
                        {
                            "code": "missing_required_input",
                            "message": message,
                            "recoverable": True,
                        }
                    ],
                },
                error=message,
                contract=contract,
            )

        output_ref = result.output_ref
        capability = IMAGE_UNDERSTANDING_CAPABILITY
        data = result.model_dump(mode="json")
        contract = build_capability_output_contract(
            capability=capability,
            status="succeeded",
            output_ref=output_ref,
            data=data,
            metadata={
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=_vision_model_observation(data),
            output_ref=output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _vision_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "summary",
        "objects",
        "people",
        "actions",
        "events",
        "colors",
        "materials",
        "scene",
        "products",
        "brands",
        "style_tags",
        "text_in_media",
        "text_in_video",
        "confidence",
        "source",
        "errors",
    )
    return {key: data[key] for key in keys if data.get(key) not in (None, "", [], {})}


def _vision_error_model_observation(error: dict[str, Any]) -> dict[str, Any]:
    message = str(error.get("message") or "Vision understanding failed.")
    return {"summary": message, "errors": [error]}
