"""Governed tool for starting image-to-3D generation."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.media.image_to_3d import (
    ImageTo3DError,
    ImageTo3DSubmission,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.image_to_3d.models import (
    ImageTo3DRequest,
    ImageTo3DResult,
)


IMAGE_TO_3D_TOOL_NAME = "image_to_3d"


class ImageTo3DStarter(Protocol):
    def start(
        self,
        *,
        session_id: str,
        src_image: str,
        output_format: str,
    ) -> ImageTo3DSubmission: ...


class MockImageTo3DAdapter:
    def start(
        self,
        *,
        session_id: str,
        src_image: str,
        output_format: str,
    ) -> ImageTo3DSubmission:
        _ = (src_image, output_format)
        return ImageTo3DSubmission(
            status="generating",
            media_id="mock-generated-image.png",
            response={
                "errCode": 0,
                "errMessage": "success",
                "data": {"status": "generating", "sessionId": session_id},
            },
        )


class ImageTo3DTool(ToolBase):
    name = IMAGE_TO_3D_TOOL_NAME
    description = "将当前媒体图片提交为3D模型或视频生成任务。"
    input_schema = ImageTo3DRequest
    output_schema = ImageTo3DResult
    category = "generate"

    def __init__(self, adapter: ImageTo3DStarter) -> None:
        self.adapter = adapter

    def _run(self, input: ImageTo3DRequest, context: ToolContext) -> ToolResult:
        request_metadata = context.metadata.get("request_metadata")
        if (
            not isinstance(request_metadata, dict)
            or request_metadata.get("transport") != "agent_service_websocket"
        ):
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="image_to_3d requires Agent-Service WebSocket entry",
                model_observation={
                    "status": "failed",
                    "message": "当前入口没有可用的媒体中继连接。",
                },
            )
        if not context.session_id:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="image_to_3d requires runtime session identity",
                model_observation={"status": "failed", "message": "缺少会话身份，无法生成3D模型。"},
            )
        try:
            submission = self.adapter.start(
                session_id=context.session_id,
                src_image=input.src_image,
                output_format=input.format,
            )
        except ImageTo3DError as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                data={"status": "failed", "result": str(exc)},
                model_observation={"status": "failed", "message": str(exc)},
            )
        data = {
            "status": submission.status,
            "media_id": submission.media_id,
        }
        if submission.status == "failed":
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="3D生成服务返回失败状态",
                data=data,
                model_observation={
                    "status": "failed",
                    "message": "3D生成服务未能接收当前任务。",
                },
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation={
                "status": submission.status,
                "message": "3D生成任务已接收，正在生成。",
            },
        )
