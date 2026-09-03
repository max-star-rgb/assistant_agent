"""Image-to-3D tool schemas."""

from pydantic import BaseModel, Field


class ImageTo3DRequest(BaseModel):
    src_image: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "image_generation 返回的 artifact:// 图片引用。"
            "同一轮已调用 image_generation 时应省略，运行时会自动使用最近生成的图片。"
        ),
    )


class ImageTo3DResult(BaseModel):
    status: str
    source_image_id: str | None = None
    job_id: str | None = None
