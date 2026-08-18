"""Native function Tool for user-uploaded image and explicit-video analysis."""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.media.video.video_adapter import VideoUnderstandingAdapter
from assistant_agent.media.video.video_context import VideoContextStore
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.media.vision.observability import observe_vision_inference
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
    sanitize_error_message,
)
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.runtime import ToolContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.ids import (
    IMAGE_UNDERSTANDING_CAPABILITY,
    UPLOADED_MEDIA_INSPECT_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)


class UploadedMediaInspector:
    """Execute uploaded image or explicit-video understanding."""

    def __init__(
        self,
        *,
        client: VisionUnderstandingClient,
        video_branch: VideoUnderstandingBranch,
    ) -> None:
        self.client = client
        self.adapter = getattr(client, "image_adapter", None)
        self.video_branch = video_branch

    def inspect(
        self,
        request: VisionUnderstandingRequest,
        context: ToolContext,
    ) -> ToolResult:
        if vision_request_has_video(request):
            result = self.video_branch.execute(
                video_request_from_vision_request(request),
                context,
            )
            return result.model_copy(
                update={"tool_name": UPLOADED_MEDIA_INSPECT_TOOL_NAME}
            )
        return self._inspect_images(request, context)

    def _inspect_images(
        self,
        request: VisionUnderstandingRequest,
        context: ToolContext,
    ) -> ToolResult:
        try:
            result = observe_vision_inference(
                lambda: self.client.understand(request),
                context=context,
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
                source="request_image",
                media_kind="image",
                media_count=len(request.image_ids),
            )
        except ProviderAdapterError as exc:
            provider = getattr(
                getattr(self.adapter, "config", None),
                "provider",
                "unknown",
            )
            error = build_provider_error(
                exc.code,
                exc.message,
                provider=provider,
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
            )
            contract = build_capability_output_contract(
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
                status="failed",
                errors=[error.model_dump(mode="json")],
            )
            return ToolResult(
                tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
                success=False,
                model_observation=_vision_error_model_observation(
                    error.model_dump(mode="json")
                ),
                error=f"{error.code}: {error.message}",
                contract=contract,
            )
        except ValueError as exc:
            message = build_provider_error(
                "provider_request_invalid",
                str(exc),
                recoverable=True,
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
                tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
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
        return _vision_tool_result(result)


def create_uploaded_media_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    video_adapter: VideoUnderstandingAdapter | None = None,
    context_store: VideoContextStore | None = None,
) -> BaseTool:
    """Create the native Tool while retaining one process-owned VLM client."""

    inspector = UploadedMediaInspector(
        client=client,
        video_branch=VideoUnderstandingBranch(
            tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
            client=client,
            adapter=video_adapter,
            context_store=context_store,
        ),
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
        context = ToolContext(
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=getattr(execution, "run_id", None),
            metadata={
                "entry_profile": runtime.context.entry_profile,
                "media_source": "uploaded",
            },
        )
        try:
            result = inspector.inspect(request, context)
        except ToolException:
            raise
        except Exception as exc:  # noqa: BLE001 - native Tool boundary.
            raise ToolException(sanitize_error_message(exc)) from exc
        if not result.success:
            raise ToolException(
                result.error or f"{UPLOADED_MEDIA_INSPECT_TOOL_NAME} failed"
            )
        observation = result.model_observation or result.data or {
            "status": "succeeded"
        }
        return (
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        observation,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ),
                }
            ],
            dict(result.data or {}),
        )

    uploaded_media_inspect.metadata = {
        "effect": "read",
        "source": "builtin",
        "availability": ToolAvailability.UPLOADED_MEDIA_PRESENT.value,
    }
    return uploaded_media_inspect


def _vision_tool_result(result: VisionUnderstandingResult) -> ToolResult:
    data = result.model_dump(mode="json")
    contract = build_capability_output_contract(
        capability=IMAGE_UNDERSTANDING_CAPABILITY,
        status="succeeded",
        output_ref=result.output_ref,
        data=data,
        metadata={
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
        },
    )
    return ToolResult(
        tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
        success=True,
        data=data,
        model_observation=_vision_model_observation(data),
        output_ref=result.output_ref,
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
        "media_kind",
        "media_refs",
        "errors",
    )
    return {
        key: data[key]
        for key in keys
        if data.get(key) not in (None, "", [], {})
    }


def _vision_error_model_observation(error: dict[str, Any]) -> dict[str, Any]:
    message = str(error.get("message") or "Vision understanding failed.")
    return {"summary": message, "errors": [error]}


__all__ = ["UploadedMediaInspector", "create_uploaded_media_inspect_tool"]
