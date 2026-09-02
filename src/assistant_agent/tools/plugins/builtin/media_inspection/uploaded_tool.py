"""Native function Tool for user-uploaded image and explicit-video analysis."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.media.video.video_adapter import VideoUnderstandingAdapter
from assistant_agent.media.video.video_context import VideoContextStore
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
)
from assistant_agent.media.vision.observability import (
    invoke_native_vision_model,
    observe_vision_inference,
)
from assistant_agent.media.vision.vision_client import (
    VisionUnderstandingClient,
    video_request_from_vision_request,
    vision_request_has_video,
)
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.providers.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.ids import (
    IMAGE_UNDERSTANDING_CAPABILITY,
    UPLOADED_MEDIA_INSPECT_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)
from assistant_agent.media.video.understanding_service import (
    VideoUnderstandingService,
)


def create_uploaded_media_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    video_adapter: VideoUnderstandingAdapter | None = None,
    context_store: VideoContextStore | None = None,
) -> BaseTool:
    """Create the native Tool while retaining one process-owned VLM client."""

    video_service = VideoUnderstandingService(
        client=client,
        adapter=video_adapter,
        context_store=context_store,
    )

    @tool(
        UPLOADED_MEDIA_INSPECT_TOOL_NAME,
        response_format="content_and_artifact",
    )
    def uploaded_media_inspect(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="需要从当前上传图片或视频中重点回答的问题。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """分析当前请求中由用户主动上传的图片或视频附件。"""

        def inspect_uploaded_media() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            state = runtime.state if isinstance(runtime.state, dict) else {}
            media = latest_runtime_media(state)
            if not media.has_uploaded_media:
                raise ToolException(
                    "uploaded_media_required: 当前请求没有用户主动上传的图片或视频"
                )
            execution = runtime.execution_info
            request = VisionUnderstandingRequest(
                image_ids=list(media.uploaded_image_ids),
                video_ids=list(media.uploaded_video_ids),
                question=question,
                user_query=media.text,
                user_id=authenticated_user_identity(runtime),
                session_id=getattr(execution, "thread_id", None),
                metadata={"media_source": "uploaded"},
                memory_context=list(state.get("memory_context", ())) or None,
            )
            if vision_request_has_video(request):
                outcome = video_service.inspect(
                    video_request_from_vision_request(request)
                )
                if outcome.status == "failed":
                    raise ToolException(outcome.error or "uploaded media inspection failed")
                return native_content_and_artifact(outcome.model_observation, outcome.data)
            try:
                if getattr(client, "traces_as_chat_model", False):
                    result = invoke_native_vision_model(
                        lambda config: client.understand(request, config=config),
                        context=None,
                        capability=IMAGE_UNDERSTANDING_CAPABILITY,
                        source="request_image",
                        media_kind="image",
                        media_count=len(request.image_ids),
                        query_provided=bool(request.question or request.user_query),
                    )
                else:
                    result = observe_vision_inference(
                        lambda: client.understand(request), context=None,
                        capability=IMAGE_UNDERSTANDING_CAPABILITY, source="request_image",
                        media_kind="image", media_count=len(request.image_ids),
                    )
            except ProviderAdapterError as exc:
                adapter_config = getattr(
                    getattr(client, "image_adapter", None), "config", None
                )
                error = build_provider_error(
                    exc.code,
                    exc.message,
                    provider=getattr(adapter_config, "provider", "unknown"),
                    capability=IMAGE_UNDERSTANDING_CAPABILITY,
                )
                raise ToolException(f"{error.code}: {error.message}") from exc
            except ValueError as exc:
                raise native_tool_exception(exc, tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME) from exc
            data = result.model_dump(mode="json")
            return native_content_and_artifact(_vision_model_observation(data), data)

        try:
            return inspect_uploaded_media()
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(
                exc, tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME
            ) from exc

    return configure_builtin_tool(
        uploaded_media_inspect,
        availability=ToolAvailability.UPLOADED_MEDIA_PRESENT.value,
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
        "media_kind",
        "media_refs",
        "errors",
    )
    return {
        key: data[key]
        for key in keys
        if data.get(key) not in (None, "", [], {})
    }

__all__ = ["create_uploaded_media_inspect_tool"]
