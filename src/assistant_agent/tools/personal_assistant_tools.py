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
    ReminderCreateRequest,
    ReminderCreateResult,
    WeatherRequest,
    WeatherResult,
)
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    DataPolicy,
    ExecutionPolicy,
    RealtimeToolPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    VisibilityPolicy,
)
from assistant_agent.services.personal_assistant_adapters import (
    CalendarAdapter,
    ContactsAdapter,
    MockCalendarAdapter,
    MockContactsAdapter,
    MockReminderAdapter,
    MockWeatherAdapter,
    ReminderAdapter,
    WeatherAdapter,
)
from assistant_agent.tools.base import MockTool, ToolContext


class WeatherTool(MockTool):
    """Look up weather through the configured personal weather adapter."""

    name = "weather"
    description = "Look up current or short-range weather for a location."
    input_schema = WeatherRequest
    output_schema = WeatherResult
    execution = ToolExecutionPolicy(
        dependency_mode="independent",
        resource_reads=["weather.forecast"],
        realtime_safety="safe",
        artifact_reuse="reusable",
        progress_message="我查一下天气。",
    )
    policy = ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="inline"),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=3, retry_count=0, max_result_chars=1600),
        data=DataPolicy(sends_data_external=True, redact_in_trace=True),
        visibility=VisibilityPolicy(toolset="personal.readonly", tags=["weather", "天气"]),
    )

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


class CalendarSearchTool(MockTool):
    """Search calendar events through the configured calendar adapter."""

    name = "calendar_search"
    description = "Search the user's calendar events."
    input_schema = CalendarSearchRequest
    output_schema = CalendarSearchResult
    execution = ToolExecutionPolicy(
        dependency_mode="independent",
        resource_reads=["calendar.events"],
        realtime_safety="safe",
        artifact_reuse="reusable",
        progress_message="我查一下日历。",
    )
    policy = ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="blocking"),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=5, retry_count=0, max_result_chars=2400),
        data=DataPolicy(
            reads_private_data=True,
            sends_data_external=True,
            redact_in_trace=True,
        ),
        visibility=VisibilityPolicy(
            toolset="personal.calendar",
            tags=["calendar", "日历", "meeting", "会议"],
        ),
    )

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


class CalendarCreateTool(MockTool):
    """Create calendar events after ToolExecutor confirmation."""

    name = "calendar_create"
    description = "Create a calendar event after explicit user confirmation."
    input_schema = CalendarCreateRequest
    output_schema = CalendarCreateResult
    execution = ToolExecutionPolicy(
        dependency_mode="terminal",
        resource_reads=["calendar.events"],
        resource_writes=["calendar.events"],
        realtime_safety="needs_confirmation",
        artifact_reuse="do_not_reuse",
        progress_message="需要你确认后我再创建日程。",
    )
    policy = ToolPolicyMetadata(
        risk="external_write",
        realtime=RealtimeToolPolicy(
            mode="confirm_then_execute",
            interruptible=False,
            commit_boundary="external_commit",
        ),
        approval=ApprovalPolicy(mode="always", confirmation_kind="calendar_write"),
        execution=ExecutionPolicy(timeout_s=8, retry_count=0, idempotency="required"),
        data=DataPolicy(
            reads_private_data=True,
            writes_private_data=True,
            sends_data_external=True,
            redact_in_trace=True,
        ),
        visibility=VisibilityPolicy(
            toolset="personal.calendar",
            tags=["calendar", "日历", "meeting", "会议"],
        ),
    )

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


class ContactsSearchTool(MockTool):
    """Search personal contacts through the configured contacts adapter."""

    name = "contacts_search"
    description = "Search the user's contacts for candidate people or contact details."
    input_schema = ContactsSearchRequest
    output_schema = ContactsSearchResult
    execution = ToolExecutionPolicy(
        dependency_mode="independent",
        resource_reads=["contacts"],
        realtime_safety="safe",
        artifact_reuse="reusable",
        progress_message="我查一下联系人。",
    )
    policy = ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="blocking"),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=5, retry_count=0, max_result_chars=2200),
        data=DataPolicy(
            reads_private_data=True,
            sends_data_external=True,
            redact_in_trace=True,
        ),
        visibility=VisibilityPolicy(toolset="personal.contacts", tags=["contacts", "联系人"]),
    )

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


class ReminderCreateTool(MockTool):
    """Create reminders after ToolExecutor confirmation."""

    name = "reminder_create"
    description = "Create a reminder or todo after explicit user confirmation."
    input_schema = ReminderCreateRequest
    output_schema = ReminderCreateResult
    execution = ToolExecutionPolicy(
        dependency_mode="terminal",
        resource_writes=["reminders"],
        realtime_safety="needs_confirmation",
        artifact_reuse="do_not_reuse",
        progress_message="需要你确认后我再创建提醒。",
    )
    policy = ToolPolicyMetadata(
        risk="external_write",
        realtime=RealtimeToolPolicy(
            mode="confirm_then_execute",
            interruptible=False,
            commit_boundary="external_commit",
        ),
        approval=ApprovalPolicy(mode="always", confirmation_kind="reminder_write"),
        execution=ExecutionPolicy(timeout_s=5, retry_count=0, idempotency="required"),
        data=DataPolicy(
            writes_private_data=True,
            sends_data_external=True,
            redact_in_trace=True,
        ),
        visibility=VisibilityPolicy(toolset="personal.reminders", tags=["todo", "reminder", "提醒"]),
    )

    def __init__(self, adapter: ReminderAdapter | None = None) -> None:
        self.adapter = adapter or MockReminderAdapter()

    def _run(self, input: ReminderCreateRequest, context: ToolContext) -> ToolResult:
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
            model_observation=_reminder_observation(result),
            output_ref=result.output_ref,
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


def _reminder_observation(result: ReminderCreateResult) -> dict[str, Any]:
    return _drop_empty(
        {
            "summary": result.summary,
            "reminder_id": result.reminder_id,
            "title": result.title,
            "due_time": result.due_time,
            "provider": result.provider,
            "errors": result.errors,
        }
    )


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, [], {})}
