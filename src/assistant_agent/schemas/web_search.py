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
    """网页搜索 Provider 的输入。"""

    query: str = Field(
        min_length=1,
        description="用于搜索当前或时效性网页信息的查询词。",
    )
    recency_days: int | None = Field(
        default=None, ge=1, le=3650, description="可选的结果时效窗口，单位为天。"
    )
    site_filter: str | None = Field(
        default=None, description="可选的网站或域名过滤条件。"
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="最多返回的搜索结果数量。",
    )
