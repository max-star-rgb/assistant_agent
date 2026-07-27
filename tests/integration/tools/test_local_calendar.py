"""Local SQLite calendar persistence and plugin wiring."""

import assistant_agent.tools.plugins.builtin.personal_assistant_mcp.plugin as calendar_plugin

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.models import (
    CalendarCreateRequest,
    CalendarSearchRequest,
)
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.tools import (
    CalendarCreateTool,
    CalendarSearchTool,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.registry_factory import create_default_registry


def test_local_calendar_persists_searches_and_isolates_users(tmp_path) -> None:
    path = tmp_path / "calendar.sqlite3"
    first = LocalSQLiteCalendarAdapter(path, namespace="user-a")
    before = first.snapshot()

    created = first.create(
        CalendarCreateRequest(
            title="系统评测占位",
            start_time="2026-08-05T15:00:00+08:00",
            end_time="2026-08-05T15:30:00+08:00",
            location="本地数据库",
            idempotency_key="run-calendar-create",
        )
    )
    replayed = LocalSQLiteCalendarAdapter(
        path,
        namespace="user-a",
    ).create(
        CalendarCreateRequest(
            title="系统评测占位",
            start_time="2026-08-05T15:00:00+08:00",
            idempotency_key="run-calendar-create",
        )
    )
    search = LocalSQLiteCalendarAdapter(path, namespace="user-a").search(
        CalendarSearchRequest(query="系统评测")
    )
    other_user = LocalSQLiteCalendarAdapter(path, namespace="user-b").search(
        CalendarSearchRequest(query="all")
    )
    semantic_first = first.create(
        CalendarCreateRequest(
            title="无显式幂等键",
            start_time="2026-08-06T09:00:00+08:00",
        )
    )
    semantic_replay = first.create(
        CalendarCreateRequest(
            title="无显式幂等键",
            start_time="2026-08-06T09:00:00+08:00",
        )
    )
    after = first.snapshot()

    assert created.success is True
    assert created.provider == "local_sqlite"
    assert replayed.event_id == created.event_id
    assert replayed.side_effect_level == "idempotent_replay"
    assert [event.title for event in search.events] == ["系统评测占位"]
    assert other_user.events == []
    assert {
        event["event_id"] for event in first.diff(before, after)["added"]
    } == {created.event_id, semantic_first.event_id}
    assert semantic_replay.event_id == semantic_first.event_id
    assert semantic_replay.side_effect_level == "idempotent_replay"


def test_default_registry_can_override_calendar_without_mcp(tmp_path) -> None:
    adapter = LocalSQLiteCalendarAdapter(
        tmp_path / "calendar.sqlite3",
        namespace="eval-user",
    )

    registry = create_default_registry(
        ProviderConfig(),
        calendar_adapter=adapter,
    )

    assert registry.get("calendar_search").adapter is adapter
    assert registry.get("calendar_create").adapter is adapter


def test_calendar_tools_scope_local_database_by_runtime_user(tmp_path) -> None:
    adapter = LocalSQLiteCalendarAdapter(tmp_path / "calendar.sqlite3")
    create = CalendarCreateTool(adapter)
    search = CalendarSearchTool(adapter)

    created = create.run(
        CalendarCreateRequest(
            title="用户 A 日程",
            start_time="2026-08-07T10:00:00+08:00",
        ),
        ToolContext(user_id="user-a"),
    )
    visible = search.run(
        CalendarSearchRequest(query="all"),
        ToolContext(user_id="user-a"),
    )
    isolated = search.run(
        CalendarSearchRequest(query="all"),
        ToolContext(user_id="user-b"),
    )

    assert created.success is True
    assert visible.data["events"][0]["title"] == "用户 A 日程"
    assert isolated.data["events"] == []


def test_real_registry_uses_local_calendar_without_mcp(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "real-calendar.sqlite3"
    monkeypatch.setattr(
        calendar_plugin,
        "DEFAULT_LOCAL_CALENDAR_PATH",
        str(path),
    )
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
    )

    registry = create_default_registry(
        config,
        mcp_server_configs=[],
    )

    search = registry.get("calendar_search")
    create = registry.get("calendar_create")
    assert isinstance(search.adapter, LocalSQLiteCalendarAdapter)
    assert isinstance(create.adapter, LocalSQLiteCalendarAdapter)
    assert search.adapter.path == path
    assert create.adapter.path == path
