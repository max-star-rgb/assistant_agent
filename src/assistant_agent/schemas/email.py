"""Stable contracts for governed read-only email access."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class EmailProviderError(BaseModel):
    """Provider-neutral email access failure."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class EmailSearchRequest(BaseModel):
    """Search the configured mailbox without retrieving message bodies."""

    query: str = Field(
        min_length=1,
        max_length=1_000,
        description=(
            "邮件搜索条件；Gmail Provider 支持 from:、to:、is:unread、"
            "after:、before:、has:attachment 等查询操作符。"
        ),
    )
    page_token: str | None = Field(
        default=None,
        max_length=2_000,
        description="仅在继续上一页搜索时传入上次结果返回的 next_page_token。",
    )
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class EmailSearchMatch(BaseModel):
    """One provider-neutral mailbox search match."""

    message_id: str = Field(min_length=1)
    thread_id: str | None = None


class EmailSearchResult(BaseModel):
    """Bounded mailbox search result used to select messages for reading."""

    success: bool
    query_used: str = Field(min_length=1)
    matches: list[EmailSearchMatch] = Field(default_factory=list)
    next_page_token: str | None = None
    summary: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    latency_ms: int = Field(default=0, ge=0)
    output_ref: str = Field(min_length=1)
    errors: list[EmailProviderError] = Field(default_factory=list)


class EmailReadRequest(BaseModel):
    """Read a small, explicitly selected batch of messages."""

    message_ids: list[str] = Field(
        min_length=1,
        max_length=5,
        description="从 email_search 结果中选择的邮件 message_id 列表，一次最多 5 封。",
    )
    max_total_chars: int = Field(default=20_000, ge=1, le=50_000)

    @field_validator("message_ids")
    @classmethod
    def normalize_message_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(values):
            raise ValueError("message_ids must not contain blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("message_ids must not contain duplicates")
        return normalized


class EmailReadResult(BaseModel):
    """Bounded email evidence returned to the assistant for analysis."""

    success: bool
    message_ids: list[str] = Field(default_factory=list)
    content: str = ""
    content_trust: Literal["untrusted_external_content"] = (
        "untrusted_external_content"
    )
    instruction_policy: Literal["do_not_execute"] = "do_not_execute"
    original_chars: int = Field(default=0, ge=0)
    truncated: bool = False
    summary: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    latency_ms: int = Field(default=0, ge=0)
    output_ref: str = Field(min_length=1)
    errors: list[EmailProviderError] = Field(default_factory=list)
