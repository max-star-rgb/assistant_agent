"""Web search tool schemas."""

from typing import Literal

from pydantic import BaseModel, Field, computed_field


WebSearchOutcome = Literal["success", "partial", "empty", "failed"]


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

    @computed_field
    @property
    def outcome(self) -> WebSearchOutcome:
        if self.results:
            return "partial" if self.errors else "success"
        return "failed" if self.errors else "empty"

    @property
    def success(self) -> bool:
        return self.outcome != "failed"


class WebSearchRequest(BaseModel):
    """网页搜索 Provider 的输入。"""

    query: str = Field(
        min_length=1,
        description="搜索主题和必要限定词。",
    )
    recency_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="用户指定的最近天数。",
    )
    site_filter: str | None = Field(
        default=None,
        description="用户指定的来源域名。",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="最多返回的搜索结果数量。",
    )
