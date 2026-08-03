"""Image-to-3D tool schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class ImageTo3DRequest(BaseModel):
    src_image: str = Field(
        min_length=1,
        description="原始图片ID，例如 cake_001；不包含目录和 .jpg 后缀。",
    )
    format: Literal["ply", "glb", "mp4"] = "mp4"


class ImageTo3DResult(BaseModel):
    status: str
    media_id: str | None = None
