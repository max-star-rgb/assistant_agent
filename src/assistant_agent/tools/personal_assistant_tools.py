"""Personal assistant tools backed by governed adapters."""

from __future__ import annotations

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.personal_assistant import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactsSearchRequest,
    ContactsSearchResult,
    WeatherRequest,
    WeatherResult,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.personal_assistant_adapters import (
    CalendarAdapter,
    ContactsAdapter,
    MockCalendarAdapter,
    MockContactsAdapter,
    MockWeatherAdapter,
    WeatherAdapter,
)
from assistant_agent.services.tool_manifest import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
)
from assistant_agent.tools.base import ToolBase, ToolContext


class WeatherTool(ToolBase):
    """Look up weather through the configured personal weather adapter."""

    name = WEATHER_TOOL_NAME
    description = (
        "Look up current or short-range weather for a location. Pass target_date as "
        "YYYY-MM-DD when the user specifies a date."
    )
    input_schema = WeatherRequest
    output_schema = WeatherResult
    category = "read"
    toolset = "personal.readonly"
    requires_confirmation = False
    progress_message = "我查一下天气。"

    def __init__(self, adapter: WeatherAdapter | None = None) -> None:
        self.adapter = adapter or MockWeatherAdapter()

    def _run(self, input: WeatherRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.lookup(input)
        return _tool_result(
            tool_name=self.name,
            capability=self.name,
            success=result.success,
            data=result.model_dump(mode="json"),
            model_observation=_weather_observation(result),
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            errors=result.errors,
            provider=result.provider,
        )


class CalendarSearchTool(ToolBase):
    """Search calendar events through the configured calendar adapter."""

    name = CALENDAR_SEARCH_TOOL_NAME
    description = "Search the user's calendar events."
    input_schema = CalendarSearchRequest
    output_schema = CalendarSearchResult
    category = "read"
    toolset = "personal.calendar"
    requires_confirmation = False
    progress_message = "我查一下日历。"

    def __init__(self, adapter: CalendarAdapter | None = None) -> None:
        self.adapter = adapter or MockCalendarAdapter()

    def _run(self, input: CalendarSearchRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        return _tool_result(
            tool_name=self.name,
            capability=self.name,
            success=result.success,
            data=result.model_dump(mode="json"),
            model_observation=_calendar_search_observation(result),
            output_ref=result.output_ref,
            raw_data_ref=result.raw_data_ref,
            latency_ms=result.latency_ms,
            errors=result.errors,
            provider=result.provider,
        )


class CalendarCreateTool(ToolBase):
    """Create calendar events after ToolExecutor confirmation."""

    name = CALENDAR_CREATE_TOOL_NAME
    description = "Create a calendar event after explicit user confirmation."
    input_schema = CalendarCreateRequest
    output_schema = CalendarCreateResult
    category = "write"
    toolset = "personal.calendar"
    requires_confirmation = True
    progress_message = "需要你确认后我再创建日程。"

    def __init__(self, adapter: CalendarAdapter | None = None) -> None:
        self.adapter = adapter or MockCalendarAdapter()

    def _run(self, input: CalendarCreateRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.create(input)
        return _tool_result(
            tool_name=self.name,
            capability=self.name,
            success=result.success,
            data={
                **result.model_dump(mode="json"),
                "idempotency": {
                    "key": input.idempotency_key,
                    "present": input.idempotency_key is not None,
                    "required": True,
                },
            },
            model_observation=_calendar_create_observation(result),
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            errors=result.errors,
            provider=result.provider,
        )


class ContactsSearchTool(ToolBase):
    """Search personal contacts through the configured contacts adapter."""

    name = CONTACTS_SEARCH_TOOL_NAME
    description = "Search the user's contacts for candidate people or contact details."
    input_schema = ContactsSearchRequest
    output_schema = ContactsSearchResult
    category = "read"
    toolset = "personal.contacts"
    requires_confirmation = False
    progress_message = "我查一下联系人。"

    def __init__(self, adapter: ContactsAdapter | None = None) -> None:
        self.adapter = adapter or MockContactsAdapter()

    def _run(self, input: ContactsSearchRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        return _tool_result(
            tool_name=self.name,
            capability=self.name,
            success=result.success,
            data=result.model_dump(mode="json"),
            model_observation=_contacts_observation(result),
            output_ref=result.output_ref,
            raw_data_ref=result.raw_data_ref,
            latency_ms=result.latency_ms,
            errors=result.errors,
            provider=result.provider,
        )


def _tool_result(
    *,
    tool_name: str,
    capability: str,
    success: bool,
    data: dict[str, Any],
    model_observation: dict[str, Any],
    output_ref: str | None,
    latency_ms: int,
    errors: list[dict[str, object]],
    provider: str,
    raw_data_ref: str | None = None,
) -> ToolResult:
    contract = build_capability_output_contract(
        capability=capability,
        status="succeeded" if success else "failed",
        output_ref=output_ref,
        data=model_observation,
        errors=errors,
        metadata={"provider": provider, "latency_ms": latency_ms},
    )
    error = None
    if not success and errors:
        first = errors[0]
        error = f"{first.get('code', 'provider_error')}: {first.get('message', 'Tool failed.')}"
    return ToolResult(
        tool_name=tool_name,
        success=success,
        data=data,
        model_observation=model_observation,
        trace_summary={
            "summary": model_observation.get("summary"),
            "provider": provider,
        },
        audit_payload={"provider": provider, "redacted": True},
        raw_data_ref=raw_data_ref,
        error=error,
        output_ref=output_ref,
        latency_ms=latency_ms,
        contract=contract,
    )


def _weather_observation(result: WeatherResult) -> dict[str, Any]:
    return _drop_empty(
        {
            "summary": result.summary,
            "location": result.location,
            "forecast": [
                item.model_dump(mode="json", exclude_none=True)
                for item in result.forecast
            ],
            "provider": result.provider,
            "errors": result.errors,
        }
    )


def _calendar_search_observation(result: CalendarSearchResult) -> dict[str, Any]:
    return _drop_empty(
        {
            "summary": result.summary,
            "query_used": result.query_used,
            "events": [
                item.model_dump(mode="json", exclude_none=True)
                for item in result.events
            ],
            "provider": result.provider,
            "errors": result.errors,
        }
    )


def _calendar_create_observation(result: CalendarCreateResult) -> dict[str, Any]:
    return _drop_empty(
        {
            "summary": result.summary,
            "event_id": result.event_id,
            "title": result.title,
            "start_time": result.start_time,
            "provider": result.provider,
            "errors": result.errors,
        }
    )


def _contacts_observation(result: ContactsSearchResult) -> dict[str, Any]:
    return _drop_empty(
        {
            "summary": result.summary,
            "query_used": result.query_used,
            "contacts": [
                item.model_dump(mode="json", exclude_none=True)
                for item in result.contacts
            ],
            "provider": result.provider,
            "errors": result.errors,
        }
    )


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, [], {})}
