"""Schemas for personal assistant calendar, contacts, weather, and reminders."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class PersonalAssistantProviderError(BaseModel):
    """Provider boundary error for personal assistant adapters."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class WeatherRequest(BaseModel):
    """Weather lookup input."""

    location: str = Field(min_length=1, description="City, address, or location name.")
    days: int = Field(default=1, ge=1, le=7, description="Forecast horizon in days.")
    units: Literal["metric"] = "metric"


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
    """Calendar event search input."""

    query: str = Field(
        default="today",
        min_length=1,
        description="Natural-language calendar query. Defaults to today when omitted.",
    )
    start_time: str | None = None
    end_time: str | None = None
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
    """Calendar event creation input."""

    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None
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
    """Contacts search input."""

    query: str = Field(min_length=1)
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


class ReminderCreateRequest(BaseModel):
    """Reminder/todo creation input."""

    title: str = Field(min_length=1)
    due_time: str | None = None
    notes: str | None = None
    list_name: str | None = None
    idempotency_key: str | None = None


class ReminderCreateResult(BaseModel):
    """Reminder/todo creation result."""

    success: bool
    reminder_id: str | None = None
    title: str | None = None
    due_time: str | None = None
    summary: str | None = None
    side_effect_level: str | None = None
    provider: str = "mock"
    latency_ms: int = Field(default=1, ge=0)
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, object]] = Field(default_factory=list)
