"""Offline personal assistant adapters for weather, calendar, contacts, and reminders."""

from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Protocol

from assistant_agent.schemas.personal_assistant import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarEvent,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactCandidate,
    ContactsSearchRequest,
    ContactsSearchResult,
    ReminderCreateRequest,
    ReminderCreateResult,
    WeatherForecast,
    WeatherRequest,
    WeatherResult,
)


class WeatherAdapter(Protocol):
    """Weather provider boundary."""

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        """Return weather for one location."""


class CalendarAdapter(Protocol):
    """Calendar provider boundary."""

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        """Return matching calendar events."""

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        """Create one calendar event."""


class ContactsAdapter(Protocol):
    """Contacts provider boundary."""

    def search(self, request: ContactsSearchRequest) -> ContactsSearchResult:
        """Return matching contact candidates."""


class ReminderAdapter(Protocol):
    """Reminder/todo provider boundary."""

    def create(self, request: ReminderCreateRequest) -> ReminderCreateResult:
        """Create one reminder/todo."""


class MockWeatherAdapter:
    """Deterministic weather adapter for offline tests and local demos."""

    provider = "mock"

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        location = request.location.strip()
        if not location:
            return _failed_weather_result(
                provider=self.provider,
                location=request.location or "weather",
                code="weather_location_empty",
                message="weather requires location.",
            )
        start_date = request.target_date or date.today()
        forecast = [
            WeatherForecast(
                date=(start_date + timedelta(days=offset)).isoformat(),
                condition="clear",
                temperature_c=26,
                high_c=29,
                low_c=23,
                precipitation_chance=0.1,
            )
            for offset in range(request.days)
        ]
        return WeatherResult(
            success=True,
            location=location,
            query_used=f"{location} from {start_date.isoformat()}",
            forecast=forecast,
            summary=f"Weather for {location}: clear, 26 C.",
            provider=self.provider,
            output_ref=(
                f"mock://weather/{_slugify(location)}/{start_date.isoformat()}"
            ),
        )


class MockCalendarAdapter:
    """Deterministic calendar adapter for offline tests and local demos."""

    provider = "mock"

    def __init__(self) -> None:
        self.created_event_titles: list[str] = []

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        query = request.query.strip()
        if not query:
            return _failed_calendar_search_result(
                provider=self.provider,
                query_used=request.query or "calendar",
                code="calendar_query_empty",
                message="calendar_search requires query.",
            )
        event = CalendarEvent(
            event_id="mock-calendar-product-sync",
            title="Product sync",
            start_time="2026-07-20T10:00:00+08:00",
            end_time="2026-07-20T10:30:00+08:00",
            timezone="Asia/Shanghai",
            location="Conference Room A",
            attendee_count=3,
        )
        events = [event][: request.limit]
        return CalendarSearchResult(
            success=True,
            query_used=query,
            events=events,
            summary=f"Calendar search returned {len(events)} event(s).",
            provider=self.provider,
            output_ref="mock://calendar/search/today",
            raw_data_ref="mock://calendar/events/today",
        )

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        title = request.title.strip()
        if not title:
            return _failed_calendar_create_result(
                provider=self.provider,
                code="calendar_title_empty",
                message="calendar_create requires title.",
            )
        key = request.idempotency_key or _slugify(title)
        self.created_event_titles.append(title)
        return CalendarCreateResult(
            success=True,
            event_id=f"mock-calendar-{key}",
            title=title,
            start_time=request.start_time,
            summary=f"Created calendar event: {title}",
            side_effect_level="committed",
            provider=self.provider,
            output_ref=f"mock://calendar/events/{key}",
        )


class MockContactsAdapter:
    """Deterministic contacts adapter for offline tests and local demos."""

    provider = "mock"

    def search(self, request: ContactsSearchRequest) -> ContactsSearchResult:
        query = request.query.strip()
        if not query:
            return _failed_contacts_result(
                provider=self.provider,
                query_used=request.query or "contacts",
                code="contacts_query_empty",
                message="contacts_search requires query.",
            )
        contacts = [
            ContactCandidate(
                contact_id="mock-contact-alex",
                display_name="Alex Chen",
                relation="work",
                emails=["alex.chen@example.test"],
                phone_numbers=["+1-555-0101"],
            )
        ][: request.limit]
        return ContactsSearchResult(
            success=True,
            query_used=query,
            contacts=contacts,
            summary=f"Contacts search returned {len(contacts)} candidate(s).",
            provider=self.provider,
            output_ref="mock://contacts/search/alex",
            raw_data_ref="mock://contacts/alex",
        )


class MockReminderAdapter:
    """Deterministic reminder adapter for offline tests and local demos."""

    provider = "mock"

    def __init__(self) -> None:
        self.created_titles: list[str] = []

    def create(self, request: ReminderCreateRequest) -> ReminderCreateResult:
        title = request.title.strip()
        if not title:
            return _failed_reminder_result(
                provider=self.provider,
                code="reminder_title_empty",
                message="reminder_create requires title.",
            )
        key = request.idempotency_key or _slugify(title)
        self.created_titles.append(title)
        return ReminderCreateResult(
            success=True,
            reminder_id=f"mock-reminder-{key}",
            title=title,
            due_time=request.due_time,
            summary=f"Created reminder: {title}",
            side_effect_level="committed",
            provider=self.provider,
            output_ref=f"mock://reminders/{key}",
        )


class UnconfiguredWeatherAdapter:
    """Explicit non-mock adapter boundary for unavailable weather providers."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        return _failed_weather_result(
            provider=self.provider,
            location=request.location or "weather",
            code="provider_unconfigured",
            message=f"{self.provider} weather provider is missing {self.missing}.",
            output_ref=f"unconfigured://weather/{self.provider}",
        )


class UnconfiguredCalendarAdapter:
    """Explicit non-mock adapter boundary for unavailable calendar providers."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        return _failed_calendar_search_result(
            provider=self.provider,
            query_used=request.query or "calendar",
            code="provider_unconfigured",
            message=f"{self.provider} calendar provider is missing {self.missing}.",
            output_ref=f"unconfigured://calendar/search/{self.provider}",
        )

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        return _failed_calendar_create_result(
            provider=self.provider,
            code="provider_unconfigured",
            message=f"{self.provider} calendar provider is missing {self.missing}.",
            output_ref=f"unconfigured://calendar/create/{self.provider}",
        )


class UnconfiguredContactsAdapter:
    """Explicit non-mock adapter boundary for unavailable contacts providers."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def search(self, request: ContactsSearchRequest) -> ContactsSearchResult:
        return _failed_contacts_result(
            provider=self.provider,
            query_used=request.query or "contacts",
            code="provider_unconfigured",
            message=f"{self.provider} contacts provider is missing {self.missing}.",
            output_ref=f"unconfigured://contacts/{self.provider}",
        )


class UnconfiguredReminderAdapter:
    """Explicit non-mock adapter boundary for unavailable reminder providers."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def create(self, request: ReminderCreateRequest) -> ReminderCreateResult:
        return _failed_reminder_result(
            provider=self.provider,
            code="provider_unconfigured",
            message=f"{self.provider} reminder provider is missing {self.missing}.",
            output_ref=f"unconfigured://reminders/{self.provider}",
        )


def _failed_weather_result(
    *,
    provider: str,
    location: str,
    code: str,
    message: str,
    recoverable: bool = True,
    output_ref: str | None = None,
) -> WeatherResult:
    return WeatherResult(
        success=False,
        location=location,
        query_used=location,
        forecast=[],
        summary=message,
        provider=provider,
        output_ref=output_ref or f"{provider}://weather/failed",
        errors=[_error(code, message, recoverable=recoverable)],
    )


def _failed_calendar_search_result(
    *,
    provider: str,
    query_used: str,
    code: str,
    message: str,
    recoverable: bool = True,
    output_ref: str | None = None,
) -> CalendarSearchResult:
    return CalendarSearchResult(
        success=False,
        query_used=query_used,
        events=[],
        summary=message,
        provider=provider,
        output_ref=output_ref or f"{provider}://calendar/search/failed",
        errors=[_error(code, message, recoverable=recoverable)],
    )


def _failed_calendar_create_result(
    *,
    provider: str,
    code: str,
    message: str,
    recoverable: bool = True,
    output_ref: str | None = None,
) -> CalendarCreateResult:
    return CalendarCreateResult(
        success=False,
        summary=message,
        provider=provider,
        output_ref=output_ref or f"{provider}://calendar/create/failed",
        errors=[_error(code, message, recoverable=recoverable)],
    )


def _failed_contacts_result(
    *,
    provider: str,
    query_used: str,
    code: str,
    message: str,
    recoverable: bool = True,
    output_ref: str | None = None,
) -> ContactsSearchResult:
    return ContactsSearchResult(
        success=False,
        query_used=query_used,
        contacts=[],
        summary=message,
        provider=provider,
        output_ref=output_ref or f"{provider}://contacts/failed",
        errors=[_error(code, message, recoverable=recoverable)],
    )


def _failed_reminder_result(
    *,
    provider: str,
    code: str,
    message: str,
    recoverable: bool = True,
    output_ref: str | None = None,
) -> ReminderCreateResult:
    return ReminderCreateResult(
        success=False,
        summary=message,
        provider=provider,
        output_ref=output_ref or f"{provider}://reminders/failed",
        errors=[_error(code, message, recoverable=recoverable)],
    )


def _error(code: str, message: str, *, recoverable: bool) -> dict[str, object]:
    return {"code": code, "message": message, "recoverable": recoverable}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"
