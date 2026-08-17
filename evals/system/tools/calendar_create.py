"""Governed system eval for the real, isolated local calendar_create Tool."""

# ruff: noqa: E402

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarCreateTool,
)
from evals.system.common.artifacts import create_run_dir, write_json
from evals.system.tools.native_tool import NativeToolInvocation, invoke_native_tool


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / ".data"
    / "evals"
    / "system"
    / "tools"
    / "calendar"
    / "create"
)
_CALENDAR_TOOL_NAME = "calendar_create"
_EVAL_USER_ID = "system-calendar-create-eval"


class CalendarCreateEvalInput(BaseModel):
    """Synthetic calendar event fields controlled by the eval operator."""

    title: str = Field(
        default="assistant_agent 本地日历创建评测",
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


class CalendarCreateEvalArtifact(BaseModel):
    """Paths retained as evidence for one local calendar_create run."""

    run_dir: Path
    database_path: Path
    summary_path: Path
    result_path: Path


class CalendarCreateEvalResult(BaseModel):
    """Machine-readable result of the governed calendar_create execution."""

    schema_version: Literal["local_calendar_create_system_eval_v1"] = (
        "local_calendar_create_system_eval_v1"
    )
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    run_id: str
    event_id: str | None = None
    validation_codes: list[str] = Field(default_factory=list)
    tool_call_statuses: list[str] = Field(default_factory=list)
    artifact: CalendarCreateEvalArtifact


def run_local_calendar_create_eval(
    *,
    eval_input: CalendarCreateEvalInput | None = None,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> CalendarCreateEvalResult:
    """Create and replay one event through the governed Tool execution chain."""

    resolved_output_root = validate_calendar_create_output_root(
        Path(output_root)
    )
    supplied_input = eval_input or CalendarCreateEvalInput()

    run_id = f"calendar-create-eval-{uuid4().hex}"
    resolved_input = supplied_input.model_copy(
        update={"title": f"{supplied_input.title} {run_id}"}
    )
    idempotency_key = f"native:{run_id}:calendar-create-idempotent"

    run_dir = create_run_dir(
        resolved_output_root,
        domain="calendar",
        case_id=run_id,
    )
    artifact = CalendarCreateEvalArtifact(
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

    tool = CalendarCreateTool(adapter)
    validation_codes: list[str] = []
    tool_results: list[NativeToolInvocation] = []
    for attempt in (1, 2):
        validation_codes.append("native_toolnode")
        tool_results.append(
            invoke_native_tool(
                tool,
                resolved_input.model_dump(mode="python"),
                user_identity=_EVAL_USER_ID,
                thread_id=run_id,
                tool_call_id="calendar-create-idempotent",
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
        "only_calendar_create_registered": tool.name == _CALENDAR_TOOL_NAME,
        "calendar_create_is_write": tool.metadata.get("effect") == "write",
        "validations_accepted": validation_codes
        == ["native_toolnode", "native_toolnode"],
        "two_governed_tool_calls": len(tool_results) == 2,
        "tool_calls_succeeded": (
            len(tool_results) == 2
            and all(result.status == "succeeded" for result in tool_results)
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
        "persisted_event_id_matches_result": (
            isinstance(persisted_event, dict)
            and event_id is not None
            and persisted_event.get("event_id") == event_id
        ),
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
    result = CalendarCreateEvalResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        run_id=run_id,
        event_id=event_id,
        validation_codes=validation_codes,
        tool_call_statuses=[item.status for item in tool_results],
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
                {
                    "status": item.status,
                    "artifact": item.artifact,
                }
                for item in tool_results
            ],
        },
    )
    return result


def validate_calendar_create_output_root(output_root: Path) -> Path:
    """Resolve an output root confined to the calendar system-eval tree."""

    _reject_symlinked_default_output_root()
    resolved = output_root.expanduser().resolve()
    allowed_root = DEFAULT_OUTPUT_ROOT.resolve()
    if resolved != allowed_root and not resolved.is_relative_to(allowed_root):
        raise ValueError(
            "calendar_create eval output root must stay within "
            f"{allowed_root}."
        )
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("calendar_create eval output root must be a directory.")
    return resolved


def _reject_symlinked_default_output_root() -> None:
    current = DEFAULT_OUTPUT_ROOT.expanduser().absolute()
    while current != PROJECT_ROOT:
        if current.is_symlink():
            raise ValueError(
                "calendar_create eval default output root must not contain symlinks."
            )
        parent = current.parent
        if parent == current:
            raise ValueError(
                "calendar_create eval default output root must stay inside the project."
            )
        current = parent


def _result_data(result: NativeToolInvocation | None) -> dict[str, Any]:
    return result.artifact if result is not None else {}


def _non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _persisted_event_matches(
    persisted_event: Any,
    expected: CalendarCreateEvalInput,
) -> bool:
    if not isinstance(persisted_event, dict):
        return False
    expected_fields = expected.model_dump(mode="json")
    return all(
        persisted_event.get(field_name) == field_value
        for field_name, field_value in expected_fields.items()
    )


if __name__ == "__main__":
    from scripts.run_system_calendar_create_eval import main

    raise SystemExit(main())
