"""Vision understanding tool backed by an adapter."""

from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.media.vision.models import (
    LiveViewInspectRequest,
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_adapter import VideoUnderstandingAdapter
from assistant_agent.media.video.video_context import (
    DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    VideoContextStore,
)
from assistant_agent.media.vision.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
)
from assistant_agent.media.vision.vision_client import (
    AdapterVisionUnderstandingClient,
    VisionUnderstandingClient,
    video_request_from_vision_request,
    vision_request_has_video,
)
from assistant_agent.media.vision.observability import observe_vision_inference
from assistant_agent.providers.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.tools.ids import (
    IMAGE_UNDERSTANDING_CAPABILITY,
    LIVE_VIEW_INSPECT_TOOL_NAME,
    MEDIA_INSPECT_TOOL_NAME,
    REALTIME_VIDEO_OBSERVE_TOOL_NAME,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)


class MediaInspectTool(ToolBase):
    """Inspect image or explicit-video media attached to the current request."""

    name = MEDIA_INSPECT_TOOL_NAME
    description = "分析当前请求附带的图片或视频。"
    input_schema = VisionUnderstandingRequest
    output_schema = VisionUnderstandingResult
    category = "read"
    repeat_policy = "distinct_inputs"
    requires_media = ["image", "video"]
    media_scope = "attached"
    trace_content_policy = "metadata_only"
    runtime_input_bindings = (
        RuntimeInputBinding(field="image_ids", source="request", key="image_ids"),
        RuntimeInputBinding(field="video_ids", source="request", key="video_ids"),
        RuntimeInputBinding(field="video_ref", source="runtime_input"),
        RuntimeInputBinding(field="frame_refs", source="runtime_input"),
        RuntimeInputBinding(field="context_id", source="runtime_input"),
        RuntimeInputBinding(field="user_query", source="request", key="text"),
        RuntimeInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        RuntimeInputBinding(field="session_id", source="runtime_identity", key="session_id"),
        RuntimeInputBinding(field="max_frames", source="runtime_input"),
        RuntimeInputBinding(field="sample_strategy", source="runtime_input"),
        RuntimeInputBinding(field="metadata", source="runtime_input"),
        RuntimeInputBinding(field="memory_context", source="memory_context", key="text"),
    )

    def __init__(
        self,
        adapter: VisionUnderstandingAdapter | None = None,
        *,
        client: VisionUnderstandingClient | None = None,
        video_adapter: VideoUnderstandingAdapter | None = None,
        context_store: VideoContextStore | None = None,
        memory_store: RealtimeVideoMemoryStore | None = None,
        semantic_store_pool: SessionVisualSemanticStorePool | None = None,
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
            semantic_store_pool=semantic_store_pool,
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

    @property
    def semantic_store_pool(self) -> SessionVisualSemanticStorePool | None:
        return self._video_branch.semantic_store_pool

    @semantic_store_pool.setter
    def semantic_store_pool(
        self,
        value: SessionVisualSemanticStorePool | None,
    ) -> None:
        self._video_branch.semantic_store_pool = value

    def _run(self, input: VisionUnderstandingRequest, context: ToolContext) -> ToolResult:
        if vision_request_has_video(input):
            result = self._video_branch.run(
                video_request_from_vision_request(input), context
            )
            return result.model_copy(update={"tool_name": self.name})
        try:
            result = observe_vision_inference(
                lambda: self.client.understand(input),
                context=context,
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
                source="request_image",
                media_kind="image",
                media_count=len(input.image_ids),
            )
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


class LiveViewInspectTool(MediaInspectTool):
    """Inspect the latest governed snapshot from a trusted live media session."""

    name = LIVE_VIEW_INSPECT_TOOL_NAME
    description = "根据具体 query 检查当前实时画面的最新一帧并返回 VLM 回答。"
    input_schema = LiveViewInspectRequest
    repeat_policy = "distinct_inputs"
    requires_media = ["video"]
    media_scope = "live"
    llm_hidden_input_fields = ("question", "user_query")
    runtime_input_bindings = tuple(
        binding
        for binding in MediaInspectTool.runtime_input_bindings
        if binding.field != "user_query"
    )

    def _validate_input(
        self,
        input: Any,
    ) -> VisionUnderstandingRequest | LiveViewInspectRequest:
        # Keep direct internal/test callers compatible; governed LLM calls use
        # the query-required schema exposed by ToolRegistry.
        if isinstance(input, VisionUnderstandingRequest) and not isinstance(
            input,
            LiveViewInspectRequest,
        ):
            return input
        return super()._validate_input(input)

    def _run(
        self,
        input: VisionUnderstandingRequest | LiveViewInspectRequest,
        context: ToolContext,
    ) -> ToolResult:
        request = input
        if isinstance(input, LiveViewInspectRequest):
            request = VisionUnderstandingRequest.model_validate(
                {
                    **input.model_dump(mode="python", exclude={"query"}),
                    "user_query": input.query,
                }
            )
        return super()._run(request, context)


class RealtimeVideoObserveTool(MediaInspectTool):
    """Internal governed tool used only by the background frame observer."""

    name = REALTIME_VIDEO_OBSERVE_TOOL_NAME
    description = "内部实时视频帧观察工具。"
    repeat_policy = "distinct_inputs"
    requires_media = ["video"]
    media_scope = "any"


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
        "media_kind",
        "media_refs",
        "errors",
    )
    return {key: data[key] for key in keys if data.get(key) not in (None, "", [], {})}


def _vision_error_model_observation(error: dict[str, Any]) -> dict[str, Any]:
    message = str(error.get("message") or "Vision understanding failed.")
    return {"summary": message, "errors": [error]}
