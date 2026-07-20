"""Visual image search tool schemas."""

from pydantic import BaseModel, Field


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
    """Input for visual image search providers.

    v1 accepts only public HTTP(S) image references. Local paths, base64, and
    private media IDs are rejected by ActionValidator before tool execution.
    """

    image_url: str | None = Field(
        default=None,
        description="Public http or https image URL to search from.",
    )
    image_ids: list[str] = Field(
        default_factory=list,
        description="Public http or https image references. v1 uses the first image.",
    )
    query_hint: str | None = Field(
        default=None,
        description="Optional text hint to guide visual similarity search.",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of similar image results to return.",
    )


VisualImageSearchInput = VisualImageSearchRequest
