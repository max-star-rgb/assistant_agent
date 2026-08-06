"""Governed tool for starting image-to-3D generation."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.identifiers import new_prefixed_uuid7
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
        user_id: str,
        session_id: str,
        src_image: str,
        output_format: str,
    ) -> ImageTo3DSubmission: ...


class MockImageTo3DAdapter:
    def start(
        self,
        *,
        user_id: str,
        session_id: str,
        src_image: str,
        output_format: str,
    ) -> ImageTo3DSubmission:
        _ = (user_id, session_id, output_format)
        return ImageTo3DSubmission(
            status="generating",
            source_image_id=src_image,
            job_id=new_prefixed_uuid7("image-to-3d", separator="-"),
        )


class ImageTo3DTool(ToolBase):
    name = IMAGE_TO_3D_TOOL_NAME
    description = "将本地生成图片提交给3D服务；完成结果可通过任务ID查询。"
    input_schema = ImageTo3DRequest
    output_schema = ImageTo3DResult
    category = "generate"

    def __init__(self, adapter: ImageTo3DStarter) -> None:
        self.adapter = adapter

    def _run(self, input: ImageTo3DRequest, context: ToolContext) -> ToolResult:
        if not context.session_id:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="image_to_3d requires runtime session identity",
                model_observation={"status": "failed", "message": "缺少会话身份，无法生成3D模型。"},
            )
        latest_image_id = context.metadata.get("latest_generated_image_id")
        src_image = (
            latest_image_id.strip()
            if isinstance(latest_image_id, str) and latest_image_id.strip()
            else input.src_image
        )
        if not isinstance(src_image, str) or not src_image.strip():
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="image_to_3d requires a generated image",
                data={"status": "failed", "result": "请先生成图片，再生成3D。"},
                model_observation={
                    "status": "failed",
                    "message": "请先调用 image_generation 生成图片，再调用 image_to_3d。",
                },
            )
        try:
            submission = self.adapter.start(
                user_id=context.user_id or context.session_id,
                session_id=context.session_id,
                src_image=src_image,
                output_format="mp4",
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
            "source_image_id": submission.source_image_id,
        }
        if submission.job_id:
            data["job_id"] = submission.job_id
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
