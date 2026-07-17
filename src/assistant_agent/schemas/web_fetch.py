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
    """Input for fetching readable content from one web URL."""

    url: str = Field(
        min_length=1,
        pattern=r"^https?://",
        description="HTTP(S) URL to fetch or extract readable content from.",
    )
    max_chars: int = Field(
        default=6000,
        ge=1,
        le=20000,
        description="Maximum content characters returned to the model.",
    )
    content_format: WebFetchContentFormat = Field(
        default="markdown",
        description="Preferred readable content format.",
    )


WebFetchInput = WebFetchRequest
