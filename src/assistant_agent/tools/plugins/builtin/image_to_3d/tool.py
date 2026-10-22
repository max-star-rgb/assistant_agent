"""Governed tool for starting image-to-3D generation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Protocol

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.identifiers import new_prefixed_uuid7
from assistant_agent.media.image_to_3d import ImageTo3DSubmission
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.ids import IMAGE_GENERATION_TOOL_NAME
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.models import ImageTo3DRequest
from assistant_agent.runtime.thread_resources import GeneratedArtifactStore, artifact_session_scope


IMAGE_TO_3D_TOOL_NAME = "image_to_3d"


class ImageTo3DStarter(Protocol):
    def start(
        self,
        *,
        user_id: str,
        session_id: str,
        src_image: str,
        output_format: str,
        source_thread_id: str | None = None,
    ) -> ImageTo3DSubmission: ...


class MockImageTo3DAdapter:
    def start(
        self,
        *,
        user_id: str,
        session_id: str,
        src_image: str,
        output_format: str,
        source_thread_id: str | None = None,
    ) -> ImageTo3DSubmission:
        _ = (user_id, session_id, output_format)
        return ImageTo3DSubmission(
            status="generating",
            source_image_id=src_image,
            job_id=new_prefixed_uuid7("image-to-3d", separator="-"),
        )


def create_image_to_3d_tool(
    adapter: ImageTo3DStarter | None = None,
    *, artifact_store: GeneratedArtifactStore | None = None,
) -> BaseTool:
    """Create the native asynchronous image-to-3D submission Tool."""

    image_to_3d_adapter = adapter or MockImageTo3DAdapter()

    @tool(IMAGE_TO_3D_TOOL_NAME, response_format="content_and_artifact")
    def image_to_3d(
        runtime: ToolRuntime[AssistantRunContext],
        src_image: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "image_generation 返回的 /artifacts/... 图片引用。默认省略：使用当前前台会话"
                    "最近成功生成的图片；用户指定其他图片时传其引用。"
                ),
            ),
        ] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把当前会话最近成功生成的图片或指定的生成图片引用提交为异步 3D 生成任务。

        返回接收状态、源图片 ID 和 job_id。只负责提交任务，不等待或伪造最终
        3D 成品。
        """

        try:
            submission = _execute_image_to_3d_from_runtime(
                image_to_3d_adapter,
                src_image,
                runtime,
                artifact_store=artifact_store,
            )
            data = {
                "status": submission.status,
                "source_image_id": submission.source_image_id,
            }
            if submission.job_id:
                data["job_id"] = submission.job_id
            if submission.status == "failed":
                raise RuntimeError("3D生成服务返回失败状态")
            return native_content_and_artifact(
                {
                    "status": submission.status,
                    "message": "3D生成任务已接收，正在生成。",
                },
                data,
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc, tool_name=IMAGE_TO_3D_TOOL_NAME) from exc

    return configure_builtin_tool(
        image_to_3d,
        bounded_expected_errors=True,
    )


def _execute_image_to_3d_from_runtime(
    adapter: ImageTo3DStarter,
    src_image: str | None,
    runtime: ToolRuntime[AssistantRunContext],
    *, artifact_store: GeneratedArtifactStore | None = None,
) -> ImageTo3DSubmission:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    identity = authenticated_user_identity(runtime)
    thread_id = runtime.execution_info.thread_id
    scope = artifact_session_scope(getattr(runtime, "config", {}))
    latest = None if src_image else (
        artifact_store.latest_image(identity, scope) if artifact_store and scope
        else _latest_generated_image_ref(state))
    selected = src_image or latest
    source_thread_id = None
    if artifact_store and selected:
        source_thread_id = artifact_store.image_source_thread(
            identity, thread_id, selected, session_scope=scope)
    return _execute_image_to_3d(
        adapter,
        ImageTo3DRequest(src_image=src_image),
        user_id=identity,
        session_id=thread_id,
        latest_image_ref=latest,
        source_thread_id=source_thread_id,
    )


def _execute_image_to_3d(
    adapter: ImageTo3DStarter,
    input: ImageTo3DRequest,
    *,
    user_id: str,
    session_id: str | None,
    latest_image_ref: str | None,
    source_thread_id: str | None = None,
) -> ImageTo3DSubmission:
    if not session_id:
        raise ValueError("image_to_3d requires runtime session identity")
    src_image = input.src_image or latest_image_ref
    if not isinstance(src_image, str) or not src_image.strip():
        raise ValueError("image_to_3d requires a generated image")
    return adapter.start(
        user_id=user_id,
        session_id=session_id,
        src_image=src_image,
        output_format="mp4",
        **({"source_thread_id": source_thread_id} if source_thread_id else {}),
    )


def _latest_generated_image_ref(state: Mapping[str, Any]) -> str | None:
    for message in reversed(state.get("messages", ())):
        if (
            not isinstance(message, ToolMessage)
            or message.name != IMAGE_GENERATION_TOOL_NAME
            or message.status == "error"
            or not isinstance(message.artifact, Mapping)
        ):
            continue
        images = message.artifact.get("images")
        if isinstance(images, (list, tuple)):
            for image in reversed(images):
                if not isinstance(image, Mapping):
                    continue
                output_ref = image.get("output_ref")
                if isinstance(output_ref, str) and output_ref.strip():
                    return output_ref.strip()
    return None
