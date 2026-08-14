"""Personal assistant tools backed by governed adapters."""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactsSearchRequest,
    ContactsSearchResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    CalendarAdapter,
    ContactsAdapter,
    MockCalendarAdapter,
    MockContactsAdapter,
)
from assistant_agent.tools.ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding


class CalendarSearchTool(ToolBase):
    """Search calendar events through the configured calendar adapter."""

    name = CALENDAR_SEARCH_TOOL_NAME
    description = (
        "按查询词和可选时间范围检索当前用户的日历；返回事件 ID、标题、起止时间、"
        "时区、地点和参与人数。只读，不创建或修改事件。"
    )
    input_schema = CalendarSearchRequest
    output_schema = CalendarSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("limit",)

    def __init__(self, adapter: CalendarAdapter | None = None) -> None:
        super().__init__()
        self.adapter = adapter or MockCalendarAdapter()

    def _execute(
        self, input: CalendarSearchRequest, context: ToolContext
    ) -> ToolResult:
        result = _calendar_adapter_for_context(self.adapter, context).search(input)
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
    """Create calendar events through the governed ToolExecutor path."""

    name = CALENDAR_CREATE_TOOL_NAME
    description = (
        "在当前用户的日历中创建事件，可设置起止时间、时区、地点、参与者和备注；"
        "返回事件 ID 与创建结果。会写入外部日历，不负责后续修改或删除。"
    )
    input_schema = CalendarCreateRequest
    output_schema = CalendarCreateResult
    category = "write"
    repeat_policy = "distinct_inputs"
    runtime_input_bindings = (
        RuntimeInputBinding(field="idempotency_key", source="durable_idempotency"),
    )

    def __init__(self, adapter: CalendarAdapter | None = None) -> None:
        super().__init__()
        self.adapter = adapter or MockCalendarAdapter()

    def _execute(
        self, input: CalendarCreateRequest, context: ToolContext
    ) -> ToolResult:
        result = _calendar_adapter_for_context(self.adapter, context).create(input)
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
    description = (
        "按姓名、关系、邮箱或电话检索当前用户的联系人；返回联系人 ID、显示名称、"
        "邮箱和电话号码。只读，不新增、修改或联系任何人。"
    )
    input_schema = ContactsSearchRequest
    output_schema = ContactsSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("limit",)

    def __init__(self, adapter: ContactsAdapter | None = None) -> None:
        super().__init__()
        self.adapter = adapter or MockContactsAdapter()

    def _execute(
        self, input: ContactsSearchRequest, context: ToolContext
    ) -> ToolResult:
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


def _calendar_adapter_for_context(
    adapter: CalendarAdapter,
    context: ToolContext,
) -> CalendarAdapter:
    resolver = getattr(adapter, "for_namespace", None)
    if not callable(resolver):
        return adapter
    namespace = context.user_id or context.session_id or context.run_id or "local"
    return resolver(namespace)


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
