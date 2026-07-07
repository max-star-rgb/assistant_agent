"""Web search tool schemas."""

from pydantic import BaseModel, Field


class WebSearchResultItem(BaseModel):
    """One normalized web search result."""

    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str = ""
    source: str | None = None
    published_at: str | None = None


class WebSearchProviderError(BaseModel):
    """Structured web search provider error."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class WebSearchResult(BaseModel):
    """Structured result returned by web search providers."""

    query_used: str = Field(min_length=1)
    results: list[WebSearchResultItem] = Field(default_factory=list)
    summary: str | None = None
    provider: str = Field(min_length=1)
    total: int = Field(default=0, ge=0)
    errors: list[WebSearchProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return not self.errors


class WebSearchRequest(BaseModel):
    """Input for web search providers."""

    query: str = Field(
        min_length=1,
        description="Search query for current or time-sensitive web information.",
    )
    recency_days: int | None = Field(
        default=None, ge=1, le=3650, description="Optional recency window in days."
    )
    site_filter: str | None = Field(
        default=None, description="Optional site or domain filter."
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of search results to return.",
    )


WebSearchInput = WebSearchRequest
