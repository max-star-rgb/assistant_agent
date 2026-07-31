"""Governed system eval for a real, isolated local calendar write."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarCreateTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.system.common.artifacts import create_run_dir, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / ".data" / "evals" / "system" / "tools" / "calendar"
)
_CALENDAR_TOOL_NAME = "calendar_create"
_EVAL_USER_ID = "system-calendar-write-eval"


class CalendarWriteEvalAuthorizationError(ValueError):
    """The operator did not explicitly authorize the local write."""


class CalendarWriteEvalInput(BaseModel):
    """Synthetic calendar event fields controlled by the eval operator."""

    title: str = Field(
        default="assistant_agent 本地日历写入评测",
        min_length=1,
    )
    start_time: str = Field(
        default="2030-01-15T09:00:00+08:00",
        min_length=1,
    )
    end_time: str | None = "2030-01-15T09:30:00+08:00"
    timezone: str | None = "Asia/Shanghai"
    location: str | None = "system-eval"
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = "synthetic system eval event"


class CalendarWriteEvalArtifact(BaseModel):
    """Paths retained as evidence for one local calendar write run."""

    run_dir: Path
    database_path: Path
    summary_path: Path
    result_path: Path


class CalendarWriteEvalResult(BaseModel):
    """Machine-readable result of the governed local calendar write."""

    schema_version: Literal["local_calendar_write_system_eval_v1"] = (
        "local_calendar_write_system_eval_v1"
    )
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    run_id: str
    event_id: str | None = None
    validation_codes: list[str] = Field(default_factory=list)
    tool_call_statuses: list[str] = Field(default_factory=list)
    artifact: CalendarWriteEvalArtifact


def run_local_calendar_write_eval(
    *,
    allow_local_calendar_write: bool,
    eval_input: CalendarWriteEvalInput | None = None,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> CalendarWriteEvalResult:
    """Create and replay one event through the governed Tool execution chain."""

    if not allow_local_calendar_write:
        raise CalendarWriteEvalAuthorizationError(
            "Local calendar write eval requires --allow-local-calendar-write."
        )
    resolved_output_root = validate_calendar_write_output_root(
        Path(output_root)
    )
    supplied_input = eval_input or CalendarWriteEvalInput()

    run_id = f"calendar-eval-{uuid4().hex}"
    request = UserRequest(
        user_id=_EVAL_USER_ID,
        session_id=run_id,
        text="执行隔离的本地日历写入 system eval。",
        task_execution_mode="foreground",
    )
    state = AgentState.from_request(request, run_id=run_id)
    resolved_input = supplied_input.model_copy(
        update={"title": f"{supplied_input.title} {run_id}"}
    )
    idempotency_key = f"{run_id}:calendar-create"

    run_dir = create_run_dir(
        resolved_output_root,
        domain="calendar",
        case_id=run_id,
    )
    artifact = CalendarWriteEvalArtifact(
        run_dir=run_dir,
        database_path=run_dir / "calendar.sqlite3",
        summary_path=run_dir / "summary.json",
        result_path=run_dir / "result.json",
    )
    adapter = LocalSQLiteCalendarAdapter(
        artifact.database_path,
        namespace=_EVAL_USER_ID,
    )
    before = adapter.snapshot()

    registry = ToolRegistry()
    registry.register(CalendarCreateTool(adapter))
    registry.seal()
    validator = ActionValidator()
    executor = ToolExecutor(registry=registry)

    validation_codes: list[str] = []
    tool_results: list[ToolResult] = []
    for attempt in (1, 2):
        decision = AssistantToolCall(
            tool_name=_CALENDAR_TOOL_NAME,
            tool_input=resolved_input.model_dump(mode="python"),
            step_id=f"calendar-write-{attempt}",
            reason="operator-authorized isolated local calendar system eval",
        )
        validation = validator.validate(
            decision=decision,
            registry=registry,
            request=request,
            state=state,
        )
        validation_codes.append(validation.code)
        if not validation.accepted:
            continue
        tool_results.append(
            executor.run_tool(
                state,
                decision.step_id or f"calendar-write-{attempt}",
                decision.tool_name,
                decision.tool_input,
                validated_input=validation.validated_input,
                runtime_input={"idempotency_key": idempotency_key},
            )
        )

    after = adapter.snapshot()
    state_diff = adapter.diff(before, after)
    persisted_events = after.get("events")
    persisted_event = (
        persisted_events[0]
        if isinstance(persisted_events, list) and len(persisted_events) == 1
        else None
    )
    first_result = tool_results[0] if len(tool_results) >= 1 else None
    second_result = tool_results[1] if len(tool_results) >= 2 else None
    first_data = _result_data(first_result)
    second_data = _result_data(second_result)
    event_id = _non_empty_string(first_data.get("event_id"))

    checks = {
        "only_calendar_create_registered": registry.list()
        == [_CALENDAR_TOOL_NAME],
        "calendar_create_is_write": (
            registry.get_spec(_CALENDAR_TOOL_NAME).category == "write"
        ),
        "validations_accepted": validation_codes == ["accepted", "accepted"],
        "two_governed_tool_calls": (
            len(state.tool_calls) == 2
            and [call.tool_name for call in state.tool_calls]
            == [_CALENDAR_TOOL_NAME, _CALENDAR_TOOL_NAME]
        ),
        "tool_calls_succeeded": (
            len(tool_results) == 2
            and all(result.success for result in tool_results)
            and [call.status for call in state.tool_calls]
            == ["succeeded", "succeeded"]
        ),
        "provider_is_local_sqlite": (
            first_data.get("provider") == "local_sqlite"
            and second_data.get("provider") == "local_sqlite"
        ),
        "first_call_committed": (
            first_data.get("side_effect_level") == "committed"
        ),
        "second_call_idempotent_replay": (
            second_data.get("side_effect_level") == "idempotent_replay"
        ),
        "same_non_empty_event_id": (
            event_id is not None and event_id == second_data.get("event_id")
        ),
        "single_persisted_event": persisted_event is not None,
        "persisted_event_matches_input": _persisted_event_matches(
            persisted_event,
            resolved_input,
        ),
        "persisted_idempotency_key_matches": (
            isinstance(persisted_event, dict)
            and persisted_event.get("idempotency_key") == idempotency_key
        ),
        "diff_has_one_addition_only": (
            len(state_diff.get("added", [])) == 1
            and state_diff.get("modified") == []
            and state_diff.get("deleted") == []
        ),
        "no_duplicate_groups": state_diff.get("duplicate_groups") == [],
    }
    failures = [name for name, passed in checks.items() if not passed]
    result = CalendarWriteEvalResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        run_id=run_id,
        event_id=event_id,
        validation_codes=validation_codes,
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
            "event_id": result.event_id,
            "artifact": artifact.model_dump(mode="json"),
        },
    )
    write_json(
        artifact.result_path,
        {
            **result.model_dump(mode="json"),
            "input": resolved_input.model_dump(mode="json"),
            "before": before,
            "after": after,
            "diff": state_diff,
            "tool_results": [
                item.model_dump(mode="json") for item in tool_results
            ],
        },
    )
    return result


def validate_calendar_write_output_root(output_root: Path) -> Path:
    """Resolve an output root confined to the calendar system-eval tree."""

    resolved = output_root.expanduser().resolve()
    allowed_root = DEFAULT_OUTPUT_ROOT.resolve()
    if resolved != allowed_root and not resolved.is_relative_to(allowed_root):
        raise ValueError(
            "Calendar write eval output root must stay within "
            f"{allowed_root}."
        )
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("Calendar write eval output root must be a directory.")
    return resolved


def _result_data(result: ToolResult | None) -> dict[str, Any]:
    if result is None or not isinstance(result.data, dict):
        return {}
    return result.data


def _non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _persisted_event_matches(
    persisted_event: Any,
    expected: CalendarWriteEvalInput,
) -> bool:
    if not isinstance(persisted_event, dict):
        return False
    expected_fields = expected.model_dump(mode="json")
    return all(
        persisted_event.get(field_name) == field_value
        for field_name, field_value in expected_fields.items()
    )
