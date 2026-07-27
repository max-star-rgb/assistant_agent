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
        description="完整的网页搜索查询，包含主题和必要限定词。",
    )
    recency_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description=(
            "仅在用户明确要求最近或过去若干天的信息时传入；"
            "表示结果时效窗口，单位为天，未指定时省略。"
        ),
    )
    site_filter: str | None = Field(
        default=None,
        description=(
            "仅在用户明确指定网站或来源域名时传入，例如“openai.com”；"
            "未指定时省略。"
        ),
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="最多返回的搜索结果数量。",
    )
