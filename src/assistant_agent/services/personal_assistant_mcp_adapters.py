"""MCP-backed adapters for stable personal assistant tools."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.adapter import MCPToolRunner, namespaced_mcp_tool_name
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.schemas.personal_assistant import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarEvent,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactCandidate,
    ContactsSearchRequest,
    ContactsSearchResult,
    WeatherForecast,
    WeatherRequest,
    WeatherResult,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.personal_assistant_adapters import (
    CalendarAdapter,
    ContactsAdapter,
    MockWeatherAdapter,
    WeatherAdapter,
)
from assistant_agent.schemas.tool_ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
)
from assistant_agent.services.provider_errors import (
    normalize_provider_error_code,
    sanitize_error_detail,
    sanitize_error_message,
)


@dataclass(frozen=True)
class MCPPersonalAssistantToolBinding:
    """One stable personal assistant capability bound to one remote MCP tool."""

    server_name: str
    tool_name: str
    namespaced_tool_name: str

    @property
    def provider(self) -> str:
        return f"mcp:{self.server_name}.{self.tool_name}"

    @property
    def output_ref(self) -> str:
        return f"mcp://{self.server_name}/{self.tool_name}"


@dataclass(frozen=True)
class PersonalAssistantAdapterBundle:
    """Adapters used when registering the stable personal assistant tools."""

    weather: WeatherAdapter
    calendar: CalendarAdapter
    contacts: ContactsAdapter


class UnconfiguredMCPPersonalAssistantAdapter:
    """Structured failure adapter for missing MCP personal assistant mappings."""

    provider = "mcp"

    def __init__(self, missing: str) -> None:
        self.missing = missing

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        return WeatherResult(
            success=False,
            location=request.location,
            query_used=request.location,
            forecast=[],
            summary=f"mcp personal assistant provider is missing {self.missing}.",
            provider=self.provider,
            output_ref="unconfigured://mcp/weather",
            errors=[_error("provider_unconfigured", self._message(), recoverable=True)],
        )

    def search(
        self,
        request: CalendarSearchRequest | ContactsSearchRequest,
    ) -> CalendarSearchResult | ContactsSearchResult:
        if isinstance(request, ContactsSearchRequest):
            return ContactsSearchResult(
                success=False,
                query_used=request.query,
                contacts=[],
                summary=self._message(),
                provider=self.provider,
                output_ref="unconfigured://mcp/contacts",
                errors=[_error("provider_unconfigured", self._message(), recoverable=True)],
            )
        return CalendarSearchResult(
            success=False,
            query_used=request.query,
            events=[],
            summary=self._message(),
            provider=self.provider,
            output_ref="unconfigured://mcp/calendar/search",
            errors=[_error("provider_unconfigured", self._message(), recoverable=True)],
        )

    def create(
        self,
        request: CalendarCreateRequest,
    ) -> CalendarCreateResult:
        return CalendarCreateResult(
            success=False,
            summary=self._message(),
            provider=self.provider,
            output_ref="unconfigured://mcp/calendar/create",
            errors=[_error("provider_unconfigured", self._message(), recoverable=True)],
        )

    def _message(self) -> str:
        return f"mcp personal assistant provider is missing {self.missing}."


class MCPPersonalAssistantWeatherAdapter:
    """Weather adapter backed by one explicitly mapped MCP tool."""

    def __init__(self, *, binding: MCPPersonalAssistantToolBinding, runner: MCPToolRunner) -> None:
        self.binding = binding
        self.runner = runner

    def lookup(self, request: WeatherRequest) -> WeatherResult:
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.binding,
            tool_input=request.model_dump(mode="json", exclude_none=True),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return WeatherResult(
                success=False,
                location=request.location,
                query_used=request.location,
                forecast=[],
                summary=result.error or "MCP weather lookup failed.",
                provider=self.binding.provider,
                latency_ms=latency_ms,
                output_ref=result.output_ref or self.binding.output_ref,
                errors=_errors_from_tool_result(result),
            )
        payload = _safe_payload(result)
        forecast = _weather_forecast_from_payload(payload)[: request.days]
        return WeatherResult(
            success=True,
            location=_text(payload, "location") or request.location,
            query_used=_text(payload, "query_used", "query") or request.location,
            forecast=forecast,
            summary=_text(payload, "summary") or f"Weather lookup returned {len(forecast)} forecast item(s).",
            provider=self.binding.provider,
            latency_ms=latency_ms,
            output_ref=result.output_ref or self.binding.output_ref,
            errors=[],
        )


class MCPPersonalAssistantCalendarAdapter:
    """Calendar adapter backed by explicitly mapped MCP tools."""

    def __init__(
        self,
        *,
        runner: MCPToolRunner,
        search_binding: MCPPersonalAssistantToolBinding | None,
        create_binding: MCPPersonalAssistantToolBinding | None,
    ) -> None:
        self.runner = runner
        self.search_binding = search_binding
        self.create_binding = create_binding

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        if self.search_binding is None:
            return UnconfiguredMCPPersonalAssistantAdapter(
                "personal_assistant_tools.calendar_search"
            ).search(request)
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.search_binding,
            tool_input=request.model_dump(mode="json", exclude_none=True),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return CalendarSearchResult(
                success=False,
                query_used=request.query,
                events=[],
                summary=result.error or "MCP calendar search failed.",
                provider=self.search_binding.provider,
                latency_ms=latency_ms,
                output_ref=result.output_ref or self.search_binding.output_ref,
                raw_data_ref=result.output_ref,
                errors=_errors_from_tool_result(result),
            )
        payload = _safe_payload(result)
        events = _calendar_events_from_payload(payload)[: request.limit]
        return CalendarSearchResult(
            success=True,
            query_used=_text(payload, "query_used", "query") or request.query,
            events=events,
            summary=_text(payload, "summary") or f"Calendar search returned {len(events)} event(s).",
            provider=self.search_binding.provider,
            latency_ms=latency_ms,
            output_ref=result.output_ref or self.search_binding.output_ref,
            raw_data_ref=result.output_ref,
            errors=[],
        )

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        if self.create_binding is None:
            return UnconfiguredMCPPersonalAssistantAdapter(
                "personal_assistant_tools.calendar_create"
            ).create(request)
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.create_binding,
            tool_input=request.model_dump(mode="json", exclude_none=True),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return CalendarCreateResult(
                success=False,
                summary=result.error or "MCP calendar create failed.",
                provider=self.create_binding.provider,
                latency_ms=latency_ms,
                output_ref=result.output_ref or self.create_binding.output_ref,
                errors=_errors_from_tool_result(result),
            )
        payload = _safe_payload(result)
        return CalendarCreateResult(
            success=True,
            event_id=_text(payload, "event_id", "id", "uid"),
            title=_text(payload, "title", "summary", "name") or request.title,
            start_time=_time_value(payload.get("start_time") or payload.get("start")) or request.start_time,
            summary=_text(payload, "summary") or f"Created calendar event: {request.title}",
            side_effect_level="committed",
            provider=self.create_binding.provider,
            latency_ms=latency_ms,
            output_ref=result.output_ref or self.create_binding.output_ref,
            errors=[],
        )


class MCPPersonalAssistantContactsAdapter:
    """Contacts adapter backed by one explicitly mapped MCP tool."""

    def __init__(self, *, binding: MCPPersonalAssistantToolBinding, runner: MCPToolRunner) -> None:
        self.binding = binding
        self.runner = runner

    def search(self, request: ContactsSearchRequest) -> ContactsSearchResult:
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.binding,
            tool_input=request.model_dump(mode="json", exclude_none=True),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return ContactsSearchResult(
                success=False,
                query_used=request.query,
                contacts=[],
                summary=result.error or "MCP contacts search failed.",
                provider=self.binding.provider,
                latency_ms=latency_ms,
                output_ref=result.output_ref or self.binding.output_ref,
                raw_data_ref=result.output_ref,
                errors=_errors_from_tool_result(result),
            )
        payload = _safe_payload(result)
        contacts = _contacts_from_payload(payload)[: request.limit]
        return ContactsSearchResult(
            success=True,
            query_used=_text(payload, "query_used", "query") or request.query,
            contacts=contacts,
            summary=_text(payload, "summary") or f"Contacts search returned {len(contacts)} candidate(s).",
            provider=self.binding.provider,
            latency_ms=latency_ms,
            output_ref=result.output_ref or self.binding.output_ref,
            raw_data_ref=result.output_ref,
            errors=[],
        )


def create_personal_assistant_adapter_bundle(
    config: ProviderConfig | None = None,
    *,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_runner: MCPToolRunner | None = None,
) -> PersonalAssistantAdapterBundle:
    """Return adapters for the stable personal assistant tools."""

    if config is None or config.provider_mode == "mock":
        return PersonalAssistantAdapterBundle(
            weather=MockWeatherAdapter(),
            calendar=_mock_calendar_adapter(),
            contacts=_mock_contacts_adapter(),
        )
    server_configs = mcp_server_configs or []
    runner = mcp_runner or _default_mcp_runner(server_configs)
    if runner is None:
        return _unconfigured_bundle("MCP server configs or runner")
    bindings = _personal_bindings(server_configs)
    calendar = (
        MCPPersonalAssistantCalendarAdapter(
            runner=runner,
            search_binding=bindings.get(CALENDAR_SEARCH_TOOL_NAME),
            create_binding=bindings.get(CALENDAR_CREATE_TOOL_NAME),
        )
        if bindings.get(CALENDAR_SEARCH_TOOL_NAME) or bindings.get(CALENDAR_CREATE_TOOL_NAME)
        else UnconfiguredMCPPersonalAssistantAdapter("personal_assistant_tools.calendar_*")
    )
    contacts = (
        MCPPersonalAssistantContactsAdapter(
            binding=bindings[CONTACTS_SEARCH_TOOL_NAME],
            runner=runner,
        )
        if bindings.get(CONTACTS_SEARCH_TOOL_NAME)
        else UnconfiguredMCPPersonalAssistantAdapter("personal_assistant_tools.contacts_search")
    )
    weather = (
        MCPPersonalAssistantWeatherAdapter(
            binding=bindings[WEATHER_TOOL_NAME],
            runner=runner,
        )
        if bindings.get(WEATHER_TOOL_NAME)
        else UnconfiguredMCPPersonalAssistantAdapter("personal_assistant_tools.weather")
    )
    return PersonalAssistantAdapterBundle(
        weather=weather,
        calendar=calendar,
        contacts=contacts,
    )


def _mock_calendar_adapter() -> CalendarAdapter:
    from assistant_agent.services.personal_assistant_adapters import MockCalendarAdapter

    return MockCalendarAdapter()


def _mock_contacts_adapter() -> ContactsAdapter:
    from assistant_agent.services.personal_assistant_adapters import MockContactsAdapter

    return MockContactsAdapter()


def _unconfigured_bundle(missing: str) -> PersonalAssistantAdapterBundle:
    adapter = UnconfiguredMCPPersonalAssistantAdapter(missing)
    return PersonalAssistantAdapterBundle(
        weather=adapter,
        calendar=adapter,
        contacts=adapter,
    )


def _default_mcp_runner(server_configs: list[MCPServerConfig]) -> MCPToolRunner | None:
    if not server_configs:
        return None
    try:
        from assistant_agent.mcp.sdk_client import SdkMCPClientRunner

        return SdkMCPClientRunner(server_configs)
    except ImportError:
        from assistant_agent.mcp.stdio_client import StdioMCPClientRunner

        return StdioMCPClientRunner(server_configs)


def _personal_bindings(
    server_configs: list[MCPServerConfig],
) -> dict[str, MCPPersonalAssistantToolBinding]:
    bindings: dict[str, MCPPersonalAssistantToolBinding] = {}
    for server in server_configs:
        mapping = server.personal_assistant_tools
        adapter_config = server.adapter_config()
        for capability, tool_name in (
            (WEATHER_TOOL_NAME, mapping.weather_lookup),
            (CALENDAR_SEARCH_TOOL_NAME, mapping.calendar_search),
            (CALENDAR_CREATE_TOOL_NAME, mapping.calendar_create),
            (CONTACTS_SEARCH_TOOL_NAME, mapping.contacts_search),
        ):
            if not tool_name or capability in bindings:
                continue
            bindings[capability] = MCPPersonalAssistantToolBinding(
                server_name=server.server_name,
                tool_name=tool_name,
                namespaced_tool_name=namespaced_mcp_tool_name(adapter_config, tool_name),
            )
    return bindings


def configured_personal_assistant_tools(
    server_configs: list[MCPServerConfig],
) -> set[str]:
    """Return stable personal tools backed by explicit real MCP mappings."""

    return set(_personal_bindings(server_configs))


def _run_mcp_tool(
    *,
    runner: MCPToolRunner,
    binding: MCPPersonalAssistantToolBinding,
    tool_input: dict[str, Any],
) -> ToolResult:
    try:
        return runner.run_tool(
            server_name=binding.server_name,
            tool_name=binding.tool_name,
            tool_input=tool_input,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return ToolResult(
            tool_name=binding.namespaced_tool_name,
            success=False,
            error=sanitize_error_message(exc),
            output_ref=binding.output_ref,
        )


def _safe_payload(result: ToolResult) -> dict[str, Any]:
    payload: Any = None
    if isinstance(result.model_observation, dict):
        payload = result.model_observation
    elif isinstance(result.data, dict):
        structured = result.data.get("structured_content")
        payload = structured if isinstance(structured, dict) else result.data
    sanitized = sanitize_error_detail(payload or {})
    return sanitized if isinstance(sanitized, dict) else {}


def _calendar_events_from_payload(payload: dict[str, Any]) -> list[CalendarEvent]:
    events = _list_value(payload, "events", "items", "results")
    normalized: list[CalendarEvent] = []
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            continue
        event_id = _text(item, "event_id", "id", "uid") or f"mcp-event-{index + 1}"
        title = _text(item, "title", "summary", "name", "subject") or "Untitled event"
        start_time = _time_value(item.get("start_time") or item.get("start") or item.get("startTime"))
        if not start_time:
            continue
        attendees = _list_value(item, "attendees", "participants")
        attendee_count = _int_value(item.get("attendee_count"))
        if attendee_count is None and attendees:
            attendee_count = len(attendees)
        normalized.append(
            CalendarEvent(
                event_id=event_id,
                title=title,
                start_time=start_time,
                end_time=_time_value(item.get("end_time") or item.get("end") or item.get("endTime")),
                timezone=_text(item, "timezone", "time_zone"),
                location=_text(item, "location", "place"),
                attendee_count=attendee_count,
            )
        )
    return normalized


def _contacts_from_payload(payload: dict[str, Any]) -> list[ContactCandidate]:
    contacts = _list_value(payload, "contacts", "candidates", "people", "items", "results")
    normalized: list[ContactCandidate] = []
    for index, item in enumerate(contacts):
        if not isinstance(item, dict):
            continue
        display_name = _text(item, "display_name", "name", "full_name")
        emails = _string_list(item.get("emails") or item.get("email"))
        phone_numbers = _string_list(item.get("phone_numbers") or item.get("phones") or item.get("phone"))
        contact_id = _text(item, "contact_id", "id", "resource_name") or (
            emails[0] if emails else f"mcp-contact-{index + 1}"
        )
        if not display_name:
            display_name = emails[0] if emails else contact_id
        normalized.append(
            ContactCandidate(
                contact_id=contact_id,
                display_name=display_name,
                relation=_text(item, "relation", "source"),
                emails=emails,
                phone_numbers=phone_numbers,
            )
        )
    return normalized


def _weather_forecast_from_payload(payload: dict[str, Any]) -> list[WeatherForecast]:
    forecast = _list_value(payload, "forecast", "forecasts", "daily")
    if not forecast and isinstance(payload.get("current"), dict):
        forecast = [payload["current"]]
    normalized: list[WeatherForecast] = []
    for index, item in enumerate(forecast):
        if not isinstance(item, dict):
            continue
        date = _text(item, "date", "day") or f"day-{index + 1}"
        condition = _text(item, "condition", "weather", "summary") or "unknown"
        temperature = _int_value(item.get("temperature_c") or item.get("temperature") or item.get("temp_c")) or 0
        normalized.append(
            WeatherForecast(
                date=date,
                condition=condition,
                temperature_c=temperature,
                high_c=_int_value(item.get("high_c") or item.get("high")),
                low_c=_int_value(item.get("low_c") or item.get("low")),
                precipitation_chance=_float_value(
                    item.get("precipitation_chance") or item.get("rain_chance")
                ),
            )
        )
    return normalized


def _errors_from_tool_result(result: ToolResult) -> list[dict[str, object]]:
    message = result.error or "MCP personal assistant tool failed."
    return [_error(normalize_provider_error_code("provider_execution_failed"), message, recoverable=True)]


def _error(code: str, message: object, *, recoverable: bool) -> dict[str, object]:
    return {
        "code": normalize_provider_error_code(code),
        "message": sanitize_error_message(message),
        "recoverable": recoverable,
    }


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_error_message(value)
        if value is not None and not isinstance(value, (dict, list)):
            text = str(value).strip()
            if text:
                return sanitize_error_message(text)
    return None


def _time_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return sanitize_error_message(value)
    if isinstance(value, dict):
        for key in ("dateTime", "date_time", "datetime", "date", "time"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return sanitize_error_message(text)
    return None


def _list_value(payload: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = sanitize_error_message(value)
        return [text] if text else []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(sanitize_error_message(item))
        return items
    return []


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
