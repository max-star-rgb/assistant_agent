"""Schemas for personal assistant calendar, contacts, and weather."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class PersonalAssistantProviderError(BaseModel):
    """Provider boundary error for personal assistant adapters."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class WeatherRequest(BaseModel):
    """天气查询输入。"""

    location: str = Field(
        min_length=1,
        description="需要查询天气的城市或区县。",
    )
    target_date: date | None = Field(
        default=None,
        description=(
            "YYYY-MM-DD；仅在用户指定日期时传。"
        ),
    )
    days: int = Field(
        default=1,
        ge=1,
        le=7,
        description="需要返回的连续预报天数；用户未指定时省略。",
    )
    units: Literal["metric"] = "metric"

    @field_validator("location", mode="before")
    @classmethod
    def _normalize_location(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class WeatherForecast(BaseModel):
    """One weather forecast item."""

    date: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    temperature_c: int
    high_c: int | None = None
    low_c: int | None = None
    precipitation_chance: float | None = Field(default=None, ge=0, le=1)


class WeatherResult(BaseModel):
    """Weather lookup result."""

    success: bool
    location: str = Field(min_length=1)
    query_used: str = Field(min_length=1)
    forecast: list[WeatherForecast] = Field(default_factory=list)
    summary: str | None = None
    provider: str = "mock"
    latency_ms: int = Field(default=1, ge=0)
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, object]] = Field(default_factory=list)


class CalendarSearchRequest(BaseModel):
    """日历事件搜索输入。"""

    query: str = Field(
        default="today",
        min_length=1,
        description="自然语言日历查询；省略时默认为今天。",
    )
    start_time: str | None = Field(
        default=None,
        description="可选的查询开始时间；仅在用户指定时间范围时传。",
    )
    end_time: str | None = Field(
        default=None,
        description="可选的查询结束时间；仅在用户指定时间范围时传。",
    )
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query", mode="before")
    @classmethod
    def _default_blank_query_to_today(cls, value: Any) -> Any:
        if value is None:
            return "today"
        if isinstance(value, str) and not value.strip():
            return "today"
        return value


class CalendarEvent(BaseModel):
    """Calendar event candidate."""

    event_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendee_count: int | None = Field(default=None, ge=0)


class CalendarSearchResult(BaseModel):
    """Calendar search result."""

    success: bool
    query_used: str = Field(min_length=1)
    events: list[CalendarEvent] = Field(default_factory=list)
    summary: str | None = None
    provider: str = "mock"
    latency_ms: int = Field(default=1, ge=0)
    output_ref: str = Field(min_length=1)
    raw_data_ref: str | None = None
    errors: list[dict[str, object]] = Field(default_factory=list)


class CalendarCreateRequest(BaseModel):
    """日历事件创建输入。"""

    title: str = Field(
        min_length=1,
        description="要创建的日历事件标题。",
    )
    start_time: str = Field(
        min_length=1,
        description="事件开始时间，使用用户表达中可确定的日期、时间和时区。",
    )
    end_time: str | None = Field(
        default=None,
        description="可选的事件结束时间；用户未指定时省略。",
    )
    timezone: str | None = Field(
        default=None,
        description="可选的事件时区；仅在用户明确指定或需要消除歧义时传。",
    )
    location: str | None = Field(
        default=None,
        description="可选的事件地点或会议位置。",
    )
    attendees: list[str] = Field(
        default_factory=list,
        description="用户明确要求邀请的联系人或邮箱列表。",
    )
    notes: str | None = Field(
        default=None,
        description="可选的事件备注或补充说明。",
    )
    idempotency_key: str | None = None


class CalendarCreateResult(BaseModel):
    """Calendar event creation result."""

    success: bool
    event_id: str | None = None
    title: str | None = None
    start_time: str | None = None
    summary: str | None = None
    side_effect_level: str | None = None
    provider: str = "mock"
    latency_ms: int = Field(default=1, ge=0)
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, object]] = Field(default_factory=list)


class ContactsSearchRequest(BaseModel):
    """联系人搜索输入。"""

    query: str = Field(
        min_length=1,
        description="用于匹配联系人姓名、关系、邮箱或电话号码的查询词。",
    )
    limit: int = Field(default=5, ge=1, le=20)


class ContactCandidate(BaseModel):
    """Contact candidate safe for model observation."""

    contact_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    relation: str | None = None
    emails: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)


class ContactsSearchResult(BaseModel):
    """Contacts search result."""

    success: bool
    query_used: str = Field(min_length=1)
    contacts: list[ContactCandidate] = Field(default_factory=list)
    summary: str | None = None
    provider: str = "mock"
    latency_ms: int = Field(default=1, ge=0)
    output_ref: str = Field(min_length=1)
    raw_data_ref: str | None = None
    errors: list[dict[str, object]] = Field(default_factory=list)
