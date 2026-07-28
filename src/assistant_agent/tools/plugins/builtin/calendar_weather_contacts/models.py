"""Schemas for personal assistant calendar, contacts, and weather."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CalendarWeatherContactsProviderError(BaseModel):
    """Provider boundary error for calendar, weather, and contacts adapters."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class WeatherRequest(BaseModel):
    """天气查询输入。"""

    location: str = Field(
        min_length=1,
        description="城市或区县；用户未提供时先追问。",
    )
    target_date: str | None = Field(
        default=None,
        description="日期 YYYY-MM-DD 或最长7天的闭区间 YYYY-MM-DD/YYYY-MM-DD。",
    )
    units: Literal["metric"] = "metric"

    @field_validator("location", mode="before")
    @classmethod
    def _normalize_location(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("target_date", mode="before")
    @classmethod
    def _normalize_target_date(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str):
            return value
        start_date, end_date = _parse_weather_date_range(value.strip())
        if start_date == end_date:
            return start_date.isoformat()
        return f"{start_date.isoformat()}/{end_date.isoformat()}"

    @property
    def date_range(self) -> tuple[date, date]:
        if self.target_date is None:
            today = date.today()
            return today, today
        return _parse_weather_date_range(self.target_date)

    @property
    def days(self) -> int:
        start_date, end_date = self.date_range
        return (end_date - start_date).days + 1


def _parse_weather_date_range(value: str) -> tuple[date, date]:
    parts = value.split("/")
    if len(parts) not in {1, 2} or any(not part for part in parts):
        raise ValueError(
            "target_date must be YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD"
        )
    try:
        start_date = date.fromisoformat(parts[0])
        end_date = date.fromisoformat(parts[-1])
    except ValueError as exc:
        raise ValueError(
            "target_date must be YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD"
        ) from exc
    if end_date < start_date:
        raise ValueError("target_date range end must not be before start")
    if (end_date - start_date).days >= 7:
        raise ValueError("target_date range must not exceed 7 days")
    return start_date, end_date


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
        description="日历查询；默认今天。",
    )
    start_time: str | None = Field(
        default=None,
        description="用户指定的查询开始时间。",
    )
    end_time: str | None = Field(
        default=None,
        description="用户指定的查询结束时间。",
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
        description="事件标题。",
    )
    start_time: str = Field(
        min_length=1,
        description="事件开始日期、时间和时区。",
    )
    end_time: str | None = Field(
        default=None,
        description="事件结束时间。",
    )
    timezone: str | None = Field(
        default=None,
        description="事件时区。",
    )
    location: str | None = Field(
        default=None,
        description="事件地点。",
    )
    attendees: list[str] = Field(
        default_factory=list,
        description="受邀联系人或邮箱。",
    )
    notes: str | None = Field(
        default=None,
        description="事件备注。",
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
        description="姓名、关系、邮箱或电话查询词。",
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
