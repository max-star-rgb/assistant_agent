"""Visual image search tool schemas."""

from pydantic import BaseModel, Field, model_validator


class VisualImageSearchMatch(BaseModel):
    """One normalized visual image search match."""

    title: str = ""
    page_url: str = ""
    image_url: str = ""
    thumbnail_url: str | None = None
    source: str | None = None
    snippet: str = ""
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)


class VisualImageSearchProviderError(BaseModel):
    """Structured visual image search provider error."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class VisualImageSearchResult(BaseModel):
    """Structured result returned by visual image search providers."""

    image_used: str = Field(min_length=1)
    query_hint_used: str | None = None
    matches: list[VisualImageSearchMatch] = Field(default_factory=list)
    provider: str = Field(min_length=1)
    total: int = Field(default=0, ge=0)
    errors: list[VisualImageSearchProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return not self.errors


class VisualImageSearchRequest(BaseModel):
    """视觉图片搜索 Provider 的输入。

    v1 只接受公开 HTTP(S) 图片引用。本地路径、base64 和私有媒体 ID
    会在工具执行前被 ActionValidator 拒绝。
    """

    image_url: str | None = Field(
        default=None,
        description="用于发起搜索的公开 HTTP 或 HTTPS 图片 URL。",
    )
    image_ids: list[str] = Field(
        default_factory=list,
        description="公开 HTTP 或 HTTPS 图片引用；v1 使用第一张图片。",
    )
    query_hint: str | None = Field(
        default=None,
        description="用于引导视觉相似搜索的可选文本提示。",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="最多返回的相似图片结果数量。",
    )

    @model_validator(mode="after")
    def require_public_image_reference(self) -> "VisualImageSearchRequest":
        refs = [self.image_url, *self.image_ids]
        normalized = [item.strip() for item in refs if isinstance(item, str) and item.strip()]
        if not normalized:
            raise ValueError("visual_image_search requires image_url or image_ids")
        if not all(_is_http_url(item) for item in normalized):
            raise ValueError(
                "visual_image_search v1 only supports public http or https image URLs"
            )
        return self


def _is_http_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


VisualImageSearchInput = VisualImageSearchRequest
