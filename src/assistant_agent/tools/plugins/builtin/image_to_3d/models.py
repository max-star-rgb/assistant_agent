"""Image-to-3D tool schemas."""

from pydantic import BaseModel, Field


class ImageTo3DRequest(BaseModel):
    src_image: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "内部图片引用，省略时自动使用本线程最近成功生成或编辑的图片；没有图片时返回错误。"
        ),
    )


class ImageTo3DResult(BaseModel):
    status: str
    source_image_id: str | None = None
    job_id: str | None = None
