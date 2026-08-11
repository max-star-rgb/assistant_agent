"""Plugin-private MCP backend for stable personal assistant tools."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.adapter import MCPToolRunner, namespaced_mcp_tool_name
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarEvent,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactCandidate,
    ContactsSearchRequest,
    ContactsSearchResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    CalendarAdapter,
    ContactsAdapter,
)
from assistant_agent.tools.ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
)
from assistant_agent.providers.provider_errors import (
    normalize_provider_error_code,
    sanitize_error_detail,
    sanitize_error_message,
)


@dataclass(frozen=True)
class MCPServiceToolBinding:
    """One stable personal assistant capability bound to one remote MCP tool."""

    server_name: str
    tool_name: str
    namespaced_tool_name: str
    profile: str = "passthrough"
    calendar_user_email: str | None = None

    @property
    def provider(self) -> str:
        return f"mcp:{self.server_name}.{self.tool_name}"

    @property
    def output_ref(self) -> str:
        return f"mcp://{self.server_name}/{self.tool_name}"


@dataclass(frozen=True)
class CalendarContactsAdapterBundle:
    """Adapters used when registering stable calendar and contacts tools."""

    calendar: CalendarAdapter
    contacts: ContactsAdapter


class UnconfiguredMCPServiceAdapter:
    """Structured failure adapter for missing MCP personal assistant mappings."""

    provider = "mcp"

    def __init__(self, missing: str) -> None:
        self.missing = missing

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


class MCPCalendarAdapter:
    """Calendar adapter backed by explicitly mapped MCP tools."""

    def __init__(
        self,
        *,
        runner: MCPToolRunner,
        search_binding: MCPServiceToolBinding | None,
        create_binding: MCPServiceToolBinding | None,
    ) -> None:
        self.runner = runner
        self.search_binding = search_binding
        self.create_binding = create_binding

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        if self.search_binding is None:
            return UnconfiguredMCPServiceAdapter(
                "personal_assistant_tools.calendar_search"
            ).search(request)
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.search_binding,
            tool_input=_calendar_search_tool_input(request, self.search_binding),
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
        payload = _calendar_search_payload(result, self.search_binding)
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
            return UnconfiguredMCPServiceAdapter(
                "personal_assistant_tools.calendar_create"
            ).create(request)
        started = time.monotonic()
        result = _run_mcp_tool(
            runner=self.runner,
            binding=self.create_binding,
            tool_input=_calendar_create_tool_input(request, self.create_binding),
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
        payload = _calendar_create_payload(result, self.create_binding)
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


class MCPContactsAdapter:
    """Contacts adapter backed by one explicitly mapped MCP tool."""

    def __init__(self, *, binding: MCPServiceToolBinding, runner: MCPToolRunner) -> None:
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


def create_calendar_contacts_adapter_bundle(
    config: ProviderConfig | None = None,
    *,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_runner: MCPToolRunner | None = None,
) -> CalendarContactsAdapterBundle:
    """Return adapters for stable calendar and contacts tools."""

    if config is None or config.provider_mode == "mock":
        return CalendarContactsAdapterBundle(
            calendar=_mock_calendar_adapter(),
            contacts=_mock_contacts_adapter(),
        )
    server_configs = mcp_server_configs or []
    runner = mcp_runner or _default_mcp_runner(server_configs)
    if runner is None:
        return _unconfigured_bundle("MCP server configs or runner")
    bindings = _personal_bindings(server_configs)
    calendar = (
        MCPCalendarAdapter(
            runner=runner,
            search_binding=bindings.get(CALENDAR_SEARCH_TOOL_NAME),
            create_binding=bindings.get(CALENDAR_CREATE_TOOL_NAME),
        )
        if bindings.get(CALENDAR_SEARCH_TOOL_NAME) or bindings.get(CALENDAR_CREATE_TOOL_NAME)
        else UnconfiguredMCPServiceAdapter("personal_assistant_tools.calendar_*")
    )
    contacts = (
        MCPContactsAdapter(
            binding=bindings[CONTACTS_SEARCH_TOOL_NAME],
            runner=runner,
        )
        if bindings.get(CONTACTS_SEARCH_TOOL_NAME)
        else UnconfiguredMCPServiceAdapter("personal_assistant_tools.contacts_search")
    )
    return CalendarContactsAdapterBundle(
        calendar=calendar,
        contacts=contacts,
    )


def _mock_calendar_adapter() -> CalendarAdapter:
    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
        MockCalendarAdapter,
    )

    return MockCalendarAdapter()


def _mock_contacts_adapter() -> ContactsAdapter:
    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
        MockContactsAdapter,
    )

    return MockContactsAdapter()


def _unconfigured_bundle(missing: str) -> CalendarContactsAdapterBundle:
    adapter = UnconfiguredMCPServiceAdapter(missing)
    return CalendarContactsAdapterBundle(
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
) -> dict[str, MCPServiceToolBinding]:
    bindings: dict[str, MCPServiceToolBinding] = {}
    for server in server_configs:
        mapping = server.personal_assistant_tools
        adapter_config = server.adapter_config()
        for capability, tool_name in (
            (CALENDAR_SEARCH_TOOL_NAME, mapping.calendar_search),
            (CALENDAR_CREATE_TOOL_NAME, mapping.calendar_create),
            (CONTACTS_SEARCH_TOOL_NAME, mapping.contacts_search),
        ):
            if not tool_name or capability in bindings:
                continue
            bindings[capability] = MCPServiceToolBinding(
                server_name=server.server_name,
                tool_name=tool_name,
                namespaced_tool_name=namespaced_mcp_tool_name(adapter_config, tool_name),
                profile=(
                    mapping.calendar_profile
                    if capability in {CALENDAR_SEARCH_TOOL_NAME, CALENDAR_CREATE_TOOL_NAME}
                    else "passthrough"
                ),
                calendar_user_email=mapping.calendar_user_email,
            )
    return bindings


def configured_calendar_contacts_tools(
    server_configs: list[MCPServerConfig],
) -> set[str]:
    """Return stable personal tools backed by explicit real MCP mappings."""

    return set(_personal_bindings(server_configs))


def _run_mcp_tool(
    *,
    runner: MCPToolRunner,
    binding: MCPServiceToolBinding,
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


def _calendar_search_tool_input(
    request: CalendarSearchRequest,
    binding: MCPServiceToolBinding,
) -> dict[str, Any]:
    if binding.profile != "workspace_mcp_v1":
        return request.model_dump(mode="json", exclude_none=True)
    return {
        "user_google_email": binding.calendar_user_email,
        "query": request.query,
        "time_min": request.start_time,
        "time_max": request.end_time,
        "max_results": request.limit,
    }


def _calendar_create_tool_input(
    request: CalendarCreateRequest,
    binding: MCPServiceToolBinding,
) -> dict[str, Any]:
    if binding.profile != "workspace_mcp_v1":
        return request.model_dump(mode="json", exclude_none=True)
    end_time = request.end_time or _default_calendar_end(request.start_time)
    return {
        "user_google_email": binding.calendar_user_email,
        "action": "create",
        "summary": request.title,
        "start_time": request.start_time,
        "end_time": end_time,
        "timezone": request.timezone,
        "location": request.location,
        "attendees": request.attendees or None,
        "description": request.notes,
    }


_WORKSPACE_EVENT_RE = re.compile(
    r'^- "(?P<title>.*?)" \(Starts: (?P<start>.*?), Ends: (?P<end>.*?)\).*?'
    r'ID: (?P<event_id>[^|\n]+)',
    re.MULTILINE,
)


def _calendar_search_payload(
    result: ToolResult,
    binding: MCPServiceToolBinding,
) -> dict[str, Any]:
    if binding.profile != "workspace_mcp_v1":
        return _safe_payload(result)
    text = _mcp_text_content(result)
    events = [
        {
            "event_id": match.group("event_id").strip(),
            "title": match.group("title").strip(),
            "start_time": match.group("start").strip(),
            "end_time": match.group("end").strip(),
        }
        for match in _WORKSPACE_EVENT_RE.finditer(text)
    ]
    return {"events": events, "summary": sanitize_error_message(text)}


def _calendar_create_payload(
    result: ToolResult,
    binding: MCPServiceToolBinding,
) -> dict[str, Any]:
    if binding.profile != "workspace_mcp_v1":
        return _safe_payload(result)
    return {"summary": sanitize_error_message(_mcp_text_content(result))}


def _mcp_text_content(result: ToolResult) -> str:
    if not isinstance(result.data, dict):
        return ""
    content = result.data.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                return text
    return ""


def _default_calendar_end(start_time: str) -> str:
    try:
        if len(start_time) == 10:
            return (date.fromisoformat(start_time) + timedelta(days=1)).isoformat()
        normalized = start_time.replace("Z", "+00:00")
        end = datetime.fromisoformat(normalized) + timedelta(hours=1)
        rendered = end.isoformat()
        return rendered.replace("+00:00", "Z") if start_time.endswith("Z") else rendered
    except ValueError:
        return start_time


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
