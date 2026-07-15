from pathlib import Path

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.context.capability_catalog import select_tool_capability_descriptors
from assistant_agent.services.context.skill_loader import load_repo_skill_descriptors
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.tools.loader import load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


def test_calendar_search_events_is_prompt_visible_only_through_valid_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_tool_module(tmp_path)
    _write_calendar_skill(tmp_path, enabled=True, include_permission=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ToolRegistry()
    register_local_tools(registry, load_local_tools(["calendar_slice_tools"]).tools)
    specs = registry.list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下我今天日历里的会议",
        metadata={"tool_visibility": {"enabled_skills": ["calendar_assistant"]}},
    )
    skill_catalog = load_repo_skill_descriptors(tmp_path)

    tool_selection = select_prompt_tool_specs(
        request,
        specs,
        skill_catalog=skill_catalog,
    )
    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=tool_selection.qualified_tool_specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        skill_catalog=skill_catalog,
    )

    assert tool_selection.summary.selected_tool_names == ["calendar.search_events"]
    assert tool_selection.summary.fallback_used is False
    assert "calendar.search_events" in capability_selection.skill_report.governed_tool_names
    assert capability_selection.skill_report.selected_skill_ids == ["calendar_assistant"]


def test_calendar_search_events_stays_hidden_without_valid_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_tool_module(tmp_path)
    _write_calendar_skill(tmp_path, enabled=False, include_permission=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ToolRegistry()
    register_local_tools(registry, load_local_tools(["calendar_slice_tools"]).tools)
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下我今天日历里的会议",
        metadata={"tool_visibility": {"enabled_skills": ["calendar_assistant"]}},
    )

    disabled_catalog = load_repo_skill_descriptors(tmp_path)
    disabled_selection = select_prompt_tool_specs(
        request,
        registry.list_specs(),
        skill_catalog=disabled_catalog,
    )

    assert disabled_selection.prompt_tool_specs == []
    assert disabled_selection.summary.selected_tool_names == []

    missing_permission_root = tmp_path / "missing_permission"
    _write_calendar_tool_module(missing_permission_root)
    _write_calendar_skill(missing_permission_root, enabled=True, include_permission=False)
    missing_catalog = load_repo_skill_descriptors(missing_permission_root)
    missing_selection = select_prompt_tool_specs(
        request,
        registry.list_specs(),
        skill_catalog=missing_catalog,
    )

    assert missing_selection.prompt_tool_specs == []
    assert missing_selection.summary.selected_tool_names == []


def test_calendar_search_events_result_redacts_trace_and_preserves_model_view(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_tool_module(tmp_path)
    _write_calendar_skill(tmp_path, enabled=True, include_permission=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ToolRegistry()
    register_local_tools(registry, load_local_tools(["calendar_slice_tools"]).tools)
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="查一下我今天日历里的会议"),
        run_id="run-1",
    )
    sink = ListEventSink()
    history = ToolHistoryStore(tmp_path / "tool_calls.jsonl")

    result = ToolExecutor(registry=registry, event_sink=sink, tool_history=history).run_tool(
        state,
        "step-1",
        "calendar.search_events",
        {"query": "today meetings"},
    )
    observation = observation_from_tool_result(result)
    finished = next(event for event in sink.events if event.type == "tool_finished")
    history_record = [record for record in history.read_all() if record.status == "succeeded"][0]

    assert result.success is True
    assert observation.structured_output["events"][0]["title"] == "Product sync"
    assert "raw-private-calendar-token" not in str(observation)
    assert "calendar-raw://events/today" not in str(observation)
    observation_summary = finished.payload["post_tool_call"]["observation_summary"]
    assert observation_summary["redacted"] is True
    assert observation_summary["trace_field_names"] == ["event_count", "summary"]
    assert "raw-private-calendar-token" not in str(finished.payload["post_tool_call"])
    assert history_record.output_summary["redacted"] is True
    assert history_record.output_summary["trace_field_names"] == [
        "event_count",
        "summary",
    ]
    assert history_record.audit_payload["redacted"] is True
    assert "raw-private-calendar-token" not in str(history_record.audit_payload)
    assert history_record.raw_data_ref == "calendar-raw://events/today"


def _write_calendar_tool_module(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calendar_slice_tools.py").write_text(
        '''
from pydantic import BaseModel, Field

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
from assistant_agent.tools.decorators import tool


class CalendarSearchInput(BaseModel):
    query: str = Field(min_length=1)


@tool(
    name="calendar.search_events",
    description="Search the user's calendar events.",
    input_schema=CalendarSearchInput,
    execution=ToolExecutionPolicy(
        dependency_mode="independent",
        resource_reads=["calendar.events"],
        realtime_safety="safe",
    ),
    policy=ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="blocking"),
        approval=ApprovalPolicy(mode="conditional"),
        execution=ExecutionPolicy(timeout_s=5, max_result_chars=2000),
        data=DataPolicy(reads_private_data=True, sends_data_external=True, redact_in_trace=True),
        visibility=VisibilityPolicy(
            toolset="personal.calendar",
            tags=["calendar", "日历"],
            enabled_by_default=False,
            skill_only=True,
        ),
    ),
)
def calendar_search_events(input, context):
    return ToolResult(
        tool_name="calendar.search_events",
        success=True,
        data={
            "summary": "Raw calendar result token raw-private-calendar-token",
            "raw_provider_payload": {"token": "raw-private-calendar-token"},
        },
        voice_summary="今天有一个产品同步会议。",
        model_observation={
            "summary": "Calendar search returned 1 event.",
            "events": [
                {
                    "title": "Product sync",
                    "time": "10:00",
                    "attendee_count": 3,
                }
            ],
        },
        trace_summary={"summary": "Calendar search returned 1 event.", "event_count": 1},
        audit_payload={"provider": "mock_calendar", "redacted": True},
        raw_data_ref="calendar-raw://events/today",
    )


__assistant_tools__ = [calendar_search_events]
'''.lstrip(),
        encoding="utf-8",
    )


def _write_calendar_skill(root: Path, *, enabled: bool, include_permission: bool) -> None:
    skill = root / "skills" / "calendar_assistant"
    skill.mkdir(parents=True, exist_ok=True)
    permissions = "- tool:calendar.search_events" if include_permission else ""
    skill.joinpath("SKILL.md").write_text(
        f"""
---
name: calendar_assistant
description: 日历查询能力，用于查看用户日程。
enabled: {str(enabled).lower()}
manifest-version: 1
---
## Governed Tools
- calendar.search_events

## Permissions
{permissions}

## When To Use
- User asks to inspect calendar events, meetings, schedule, 日历, 会议, or 日程.

## Visibility
- toolset: personal.calendar
- tags: calendar, 日历, meeting
- enabled_by_default: false
- skill_only: true
""".strip()
        + "\n",
        encoding="utf-8",
    )
