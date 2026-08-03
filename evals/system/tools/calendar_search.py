"""Governed system eval for the real, isolated local calendar_search Tool."""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.models import (
    CalendarCreateRequest,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarSearchTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.system.common.artifacts import create_run_dir, write_json


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / ".data"
    / "evals"
    / "system"
    / "tools"
    / "calendar"
    / "search"
)
_CALENDAR_TOOL_NAME = "calendar_search"
_EVAL_USER_ID = "system-calendar-search-eval"


class CalendarSearchEvalInput(BaseModel):
    """Search input and synthetic event fields controlled by the eval."""

    query: str = Field(
        default="assistant_agent 本地日历搜索评测",
        min_length=1,
    )
    seed_title: str = Field(
        default="assistant_agent 本地日历搜索评测",
        min_length=1,
    )
    start_time: str = Field(
        default="2030-01-16T10:00:00+08:00",
        min_length=1,
    )
    end_time: str | None = "2030-01-16T10:30:00+08:00"
    timezone: str | None = "Asia/Shanghai"
    location: str | None = "system-eval"


class CalendarSearchEvalArtifact(BaseModel):
    """Paths retained as evidence for one local calendar_search run."""

    run_dir: Path
    database_path: Path
    summary_path: Path
    result_path: Path


class CalendarSearchEvalResult(BaseModel):
    """Machine-readable result of the governed calendar_search execution."""

    schema_version: Literal["local_calendar_search_system_eval_v1"] = (
        "local_calendar_search_system_eval_v1"
    )
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    run_id: str
    seeded_event_id: str | None = None
    validation_code: str
    tool_call_statuses: list[str] = Field(default_factory=list)
    artifact: CalendarSearchEvalArtifact


def run_local_calendar_search_eval(
    *,
    eval_input: CalendarSearchEvalInput | None = None,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> CalendarSearchEvalResult:
    """Seed one event, then execute calendar_search through governance."""

    resolved_output_root = validate_calendar_search_output_root(
        Path(output_root)
    )
    supplied_input = eval_input or CalendarSearchEvalInput()

    run_id = f"calendar-search-eval-{uuid4().hex}"
    request = UserRequest(
        user_id=_EVAL_USER_ID,
        session_id=run_id,
        text="执行隔离的本地 calendar_search system eval。",
        task_execution_mode="foreground",
    )
    state = AgentState.from_request(request, run_id=run_id)
    resolved_seed_title = f"{supplied_input.seed_title} {run_id}"
    seed_idempotency_key = f"{run_id}:calendar-search-seed"

    run_dir = create_run_dir(
        resolved_output_root,
        domain="calendar-search",
        case_id=run_id,
    )
    artifact = CalendarSearchEvalArtifact(
        run_dir=run_dir,
        database_path=run_dir / "calendar.sqlite3",
        summary_path=run_dir / "summary.json",
        result_path=run_dir / "result.json",
    )
    adapter = LocalSQLiteCalendarAdapter(
        artifact.database_path,
        namespace=_EVAL_USER_ID,
    )
    seed_result = adapter.create(
        CalendarCreateRequest(
            title=resolved_seed_title,
            start_time=supplied_input.start_time,
            end_time=supplied_input.end_time,
            timezone=supplied_input.timezone,
            location=supplied_input.location,
            attendees=[],
            notes="synthetic calendar_search system eval seed",
            idempotency_key=seed_idempotency_key,
        )
    )
    before = adapter.snapshot()

    registry = ToolRegistry()
    registry.register(CalendarSearchTool(adapter))
    registry.seal()
    decision = AssistantToolCall(
        tool_name=_CALENDAR_TOOL_NAME,
        tool_input={
            "query": supplied_input.query,
            "start_time": supplied_input.start_time,
            "end_time": supplied_input.end_time,
        },
        step_id="calendar-search",
        reason="isolated local calendar system eval",
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    tool_result: ToolResult | None = None
    if validation.accepted:
        tool_result = ToolExecutor(registry=registry).run_tool(
            state,
            decision.step_id or "calendar-search",
            decision.tool_name,
            decision.tool_input,
            validated_input=validation.validated_input,
        )

    after = adapter.snapshot()
    result_data = _result_data(tool_result)
    returned_events = result_data.get("events")
    returned_event = (
        returned_events[0]
        if isinstance(returned_events, list) and len(returned_events) == 1
        else None
    )
    seeded_event_id = _non_empty_string(seed_result.event_id)
    checks = {
        "only_calendar_search_registered": registry.list()
        == [_CALENDAR_TOOL_NAME],
        "calendar_search_is_read": (
            registry.get_spec(_CALENDAR_TOOL_NAME).category == "read"
        ),
        "seed_event_committed": (
            seed_result.success
            and seed_result.side_effect_level == "committed"
            and seeded_event_id is not None
        ),
        "validation_accepted": validation.code == "accepted",
        "one_governed_tool_call": (
            len(state.tool_calls) == 1
            and state.tool_calls[0].tool_name == _CALENDAR_TOOL_NAME
        ),
        "tool_call_succeeded": (
            tool_result is not None
            and tool_result.success
            and [call.status for call in state.tool_calls] == ["succeeded"]
        ),
        "provider_is_local_sqlite": result_data.get("provider")
        == "local_sqlite",
        "query_used_matches": result_data.get("query_used")
        == supplied_input.query,
        "one_expected_event_returned": _returned_event_matches(
            returned_event,
            event_id=seeded_event_id,
            title=resolved_seed_title,
            eval_input=supplied_input,
        ),
        "single_seed_event_persisted": (
            len(before.get("events", [])) == 1
            and before["events"][0].get("event_id") == seeded_event_id
        ),
        "search_did_not_modify_calendar": before == after,
    }
    failures = [name for name, passed in checks.items() if not passed]
    result = CalendarSearchEvalResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        run_id=run_id,
        seeded_event_id=seeded_event_id,
        validation_code=validation.code,
        tool_call_statuses=[call.status for call in state.tool_calls],
        artifact=artifact,
    )
    write_json(
        artifact.summary_path,
        {
            "schema_version": result.schema_version,
            "passed": result.passed,
            "checks": result.checks,
            "failures": result.failures,
            "run_id": result.run_id,
            "seeded_event_id": result.seeded_event_id,
            "artifact": artifact.model_dump(mode="json"),
        },
    )
    write_json(
        artifact.result_path,
        {
            **result.model_dump(mode="json"),
            "input": supplied_input.model_dump(mode="json"),
            "resolved_seed_title": resolved_seed_title,
            "before": before,
            "after": after,
            "seed_result": seed_result.model_dump(mode="json"),
            "tool_result": (
                tool_result.model_dump(mode="json")
                if tool_result is not None
                else None
            ),
        },
    )
    return result


def validate_calendar_search_output_root(output_root: Path) -> Path:
    """Resolve an output root confined to the calendar_search eval tree."""

    _reject_symlinked_default_output_root()
    resolved = output_root.expanduser().resolve()
    allowed_root = DEFAULT_OUTPUT_ROOT.resolve()
    if resolved != allowed_root and not resolved.is_relative_to(allowed_root):
        raise ValueError(
            "calendar_search eval output root must stay within "
            f"{allowed_root}."
        )
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("calendar_search eval output root must be a directory.")
    return resolved


def _reject_symlinked_default_output_root() -> None:
    current = DEFAULT_OUTPUT_ROOT.expanduser().absolute()
    while current != PROJECT_ROOT:
        if current.is_symlink():
            raise ValueError(
                "calendar_search eval default output root must not contain symlinks."
            )
        parent = current.parent
        if parent == current:
            raise ValueError(
                "calendar_search eval default output root must stay inside the project."
            )
        current = parent


def _result_data(result: ToolResult | None) -> dict[str, Any]:
    if result is None or not isinstance(result.data, dict):
        return {}
    return result.data


def _non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _returned_event_matches(
    event: Any,
    *,
    event_id: str | None,
    title: str,
    eval_input: CalendarSearchEvalInput,
) -> bool:
    if not isinstance(event, dict) or event_id is None:
        return False
    return event == {
        "event_id": event_id,
        "title": title,
        "start_time": eval_input.start_time,
        "end_time": eval_input.end_time,
        "timezone": eval_input.timezone,
        "location": eval_input.location,
        "attendee_count": 0,
    }


if __name__ == "__main__":
    from scripts.run_system_calendar_search_eval import main

    raise SystemExit(main())
