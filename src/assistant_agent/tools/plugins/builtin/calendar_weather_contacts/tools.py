"""Personal assistant tools backed by governed adapters."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarSearchRequest,
    CalendarSearchResult,
    ContactsSearchRequest,
    ContactsSearchResult,
)
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
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_idempotency_key,
    native_tool_exception,
)


def create_calendar_search_tool(adapter: CalendarAdapter | None = None) -> BaseTool:
    """Create a native, read-only calendar event search Tool."""

    calendar_adapter = adapter or MockCalendarAdapter()

    @tool(CALENDAR_SEARCH_TOOL_NAME, response_format="content_and_artifact")
    def calendar_search(
        runtime: ToolRuntime[AssistantRunContext],
        query: Annotated[
            str,
            Field(default="today", min_length=1, description="日历查询；默认今天。"),
        ] = "today",
        start_time: Annotated[
            str | None, Field(description="用户指定的查询开始时间。")
        ] = None,
        end_time: Annotated[
            str | None, Field(description="用户指定的查询结束时间。")
        ] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按查询词和可选时间范围检索当前用户的日历。

        返回事件 ID、标题、起止时间、时区、地点和参与人数。只读，不创建或修改事件。
        """

        try:
            result = _execute_calendar_search(
                calendar_adapter,
                CalendarSearchRequest(
                    query=query,
                    start_time=start_time,
                    end_time=end_time,
                ),
                user_id=authenticated_user_identity(runtime),
            )
            _raise_result_error(result.errors, CALENDAR_SEARCH_TOOL_NAME)
            return native_content_and_artifact(
                _calendar_search_observation(result),
                result.model_dump(mode="json"),
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc) from exc

    return configure_builtin_tool(calendar_search)


def create_calendar_create_tool(adapter: CalendarAdapter | None = None) -> BaseTool:
    """Create a native calendar write Tool with runtime-owned idempotency."""

    calendar_adapter = adapter or MockCalendarAdapter()

    @tool(CALENDAR_CREATE_TOOL_NAME, response_format="content_and_artifact")
    def calendar_create(
        title: Annotated[str, Field(min_length=1, description="事件标题。")],
        start_time: Annotated[
            str,
            Field(min_length=1, description="事件开始日期、时间和时区。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        end_time: Annotated[str | None, Field(description="事件结束时间。")] = None,
        timezone: Annotated[str | None, Field(description="事件时区。")] = None,
        location: Annotated[str | None, Field(description="事件地点。")] = None,
        attendees: Annotated[list[str], Field(description="受邀联系人或邮箱。")] = [],
        notes: Annotated[str | None, Field(description="事件备注。")] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """在当前用户的日历中创建事件。

        可设置起止时间、时区、地点、参与者和备注，并返回事件 ID 与创建结果。
        会写入外部日历，不负责后续修改或删除。
        """

        try:
            request = CalendarCreateRequest(
                title=title,
                start_time=start_time,
                end_time=end_time,
                timezone=timezone,
                location=location,
                attendees=attendees,
                notes=notes,
                idempotency_key=native_idempotency_key(runtime),
            )
            result = _execute_calendar_create(
                calendar_adapter,
                request,
                user_id=authenticated_user_identity(runtime),
            )
            _raise_result_error(result.errors, CALENDAR_CREATE_TOOL_NAME)
            return native_content_and_artifact(
                _calendar_create_observation(result),
                {
                    **result.model_dump(mode="json"),
                    "idempotency": {
                        "key": request.idempotency_key,
                        "present": request.idempotency_key is not None,
                        "required": True,
                    },
                },
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc) from exc

    return configure_builtin_tool(calendar_create, bounded_expected_errors=True)


def create_contacts_search_tool(adapter: ContactsAdapter | None = None) -> BaseTool:
    """Create a native, read-only personal contacts search Tool."""

    contacts_adapter = adapter or MockContactsAdapter()

    @tool(CONTACTS_SEARCH_TOOL_NAME, response_format="content_and_artifact")
    def contacts_search(
        query: Annotated[
            str,
            Field(min_length=1, description="姓名、关系、邮箱或电话查询词。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按姓名、关系、邮箱或电话检索当前用户的联系人。

        返回联系人 ID、显示名称、邮箱和电话号码。只读，不新增、修改或联系任何人。
        """

        try:
            result = _execute_contacts_search(
                contacts_adapter,
                ContactsSearchRequest(query=query),
                user_id=authenticated_user_identity(runtime),
            )
            _raise_result_error(result.errors, CONTACTS_SEARCH_TOOL_NAME)
            return native_content_and_artifact(
                _contacts_observation(result),
                result.model_dump(mode="json"),
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc) from exc

    return configure_builtin_tool(contacts_search)


def _execute_calendar_search(
    adapter: CalendarAdapter,
    input: CalendarSearchRequest,
    *,
    user_id: str,
) -> CalendarSearchResult:
    return _calendar_adapter_for_user(adapter, user_id).search(input)


def _execute_calendar_create(
    adapter: CalendarAdapter,
    input: CalendarCreateRequest,
    *,
    user_id: str,
) -> CalendarCreateResult:
    return _calendar_adapter_for_user(adapter, user_id).create(input)


def _execute_contacts_search(
    adapter: ContactsAdapter,
    input: ContactsSearchRequest,
    *,
    user_id: str,
) -> ContactsSearchResult:
    del user_id
    return adapter.search(input)


def _calendar_adapter_for_user(
    adapter: CalendarAdapter,
    user_id: str,
) -> CalendarAdapter:
    resolver = getattr(adapter, "for_namespace", None)
    if not callable(resolver):
        return adapter
    return resolver(user_id)


def _raise_result_error(errors: list[dict[str, object]], tool_name: str) -> None:
    if not errors:
        return
    first = errors[0]
    raise ToolException(
        f"{first.get('code', 'provider_error')}: "
        f"{first.get('message', f'{tool_name} failed')}"
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
