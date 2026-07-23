"""Web fetch tool schemas."""

from typing import Literal

from pydantic import BaseModel, Field


WebFetchContentFormat = Literal["markdown", "text"]


class WebFetchProviderError(BaseModel):
    """Structured web fetch provider error."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class WebFetchResult(BaseModel):
    """Structured result returned by web fetch providers."""

    url: str = Field(min_length=1)
    title: str | None = None
    content: str = ""
    content_format: WebFetchContentFormat = "markdown"
    provider: str = Field(min_length=1)
    total_chars: int = Field(default=0, ge=0)
    truncated: bool = False
    errors: list[WebFetchProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return not self.errors


class WebFetchRequest(BaseModel):
    """从单个网页 URL 获取可读内容的输入。"""

    url: str = Field(
        min_length=1,
        pattern=r"^https?://",
        description="需要获取或提取可读内容的 HTTP(S) URL。",
    )
    max_chars: int = Field(
        default=6000,
        ge=1,
        le=20000,
        description="最多返回给模型的内容字符数。",
    )
    content_format: WebFetchContentFormat = Field(
        default="markdown",
        description="期望的可读内容格式。",
    )


WebFetchInput = WebFetchRequest
